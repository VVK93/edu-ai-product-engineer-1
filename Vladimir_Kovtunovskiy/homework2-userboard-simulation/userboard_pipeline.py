"""
Multi-Agent User Board Simulation Pipeline

This script orchestrates an end-to-end pipeline:
1. Loads clustered user review data.
2. Selects top clusters based on negative sentiment.
3. Uses an LLM to ideate features addressing pains in selected clusters.
4. Generates distinct user personas based on selected clusters using an LLM.
5. Simulates a multi-round user board discussion using LLM agents representing personas.
6. Summarizes the discussion using an LLM.
7. Writes a final Markdown report.

Setup:
1. Ensure Python 3.9+ is installed.
2. Create a virtual environment: `python -m venv .venv && source .venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt` (Create this file!)
4. Create a `.env` file in the same directory as the script with your `OPENAI_API_KEY="sk-..."`.
5. Place the cluster JSON file (e.g., `clusters_data.json`) in a `cluster_outputs` subdirectory relative to this script.
6. Run the script: `python your_script_name.py`

Outputs:
* `multiagent_outputs/board_session_report.md` - The final analysis report.
* `multiagent_outputs/board_session.log` - Detailed execution log.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple, TypedDict

import numpy as np # Added for seeding
from dotenv import load_dotenv
from langchain.schema import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_community.callbacks.manager import get_openai_callback # Optional: for cost tracking
from langchain_core.exceptions import LangChainException # For broader error catching
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from rich.logging import RichHandler
# Added for retry logic
from tenacity import retry, stop_after_attempt, wait_fixed
from openai import APIError # Specific error type
from langchain.chains import ConversationChain
from langchain.memory import ConversationBufferWindowMemory
from langchain_core.prompts import PromptTemplate # Use standard PromptTemplate
from langchain.tools import Tool # Example tool import, might not be needed initially
from collections import defaultdict # Added import

# --- Determine Project Root and Load Env ---
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR

env_path = PROJECT_ROOT / ".env"
if not env_path.exists():
    print(f"Warning: .env file not found at {env_path}. Ensure OPENAI_API_KEY is set via environment variables.")
load_dotenv(dotenv_path=env_path)

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """Project-wide constants. Adjust as desired."""

    # IO - Paths relative to the PROJECT_ROOT defined above
    cluster_json: Path = PROJECT_ROOT / "cluster_outputs" / "clusters_data.json"
    output_dir: Path = PROJECT_ROOT / "multiagent_outputs"

    # LLM - Use a valid OpenAI model name
    model_name: str = "gpt-4-turbo" # Example: Use a valid model
    temperature: float = 0.7

    # Simulation
    persona_count: int = 5
    feature_count: int = 3 # Define number of features to ideate
    discussion_rounds: int = 3

    # Determinism - Set to None to disable seeding
    random_seed: int | None = 42


CFG = Config()
CFG.output_dir.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# Logging – pretty console + file
# -----------------------------------------------------------------------------
log_path = CFG.output_dir / "board_session.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(module)s:%(lineno)d | %(message)s", # Added module/lineno
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False), # show_path=False for cleaner console
              logging.FileHandler(log_path, "w", "utf-8")],
    force=True # Override root logger config if necessary
)
# Reduce verbosity of noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

logger = logging.getLogger(__name__) # Get logger for this module
logger.info("Logging initialised. Log file: %s", log_path)
logger.info("Using configuration: %s", CFG)


# -----------------------------------------------------------------------------
# Utility Dataclasses & State Definition
# -----------------------------------------------------------------------------
@dataclass
class FeatureProposal:
    id: int
    description: str

    def md(self) -> str:
        return f"{self.id}. {self.description}"


@dataclass
class Persona:
    name: str
    background: str
    quote: str
    sentiment: str  # positive / neutral / negative
    pain_points: List[str]
    inspired_by_cluster_id: str | None = None

    @property
    def system_prompt(self) -> str:
        """Generates the system prompt for the LLM agent representing this persona."""
        pain_str = "; ".join(self.pain_points) # Use semicolon for clarity if needed
        return (
            f"You are {self.name}. Act and respond authentically based on this profile:\n"
            f"- Background: {self.background}\n"
            f"- Overall Sentiment towards Spotify: {self.sentiment}\n"
            f"- Key Pain Points/Frustrations: {pain_str}\n"
            f"Always speak in the first person ('I', 'me', 'my'). Keep your responses concise (1-3 sentences unless asked otherwise) and focused on the discussion topic. "
            f"Ground your opinions in your background and pain points. Be honest and natural."
        )

    def md(self) -> str:
        """Returns a Markdown representation of the persona."""
        pain_str = "\n  - ".join(self.pain_points)
        return (
            f"### {self.name}\n\n"
            f"*{self.quote}*\n\n"
            f"**Background**: {self.background}\n\n"
            f"**Sentiment**: {self.sentiment.capitalize()}\n\n"
            f"**Key Pain Points**:\n  - {pain_str}\n"
        )


class AgentState(TypedDict):
    """Defines the state passed between graph nodes."""
    selected_clusters: Dict[str, dict]
    features: List[FeatureProposal]
    personas: List[Persona]
    transcript: str # Markdown transcript
    conversation_history: List[BaseMessage] # Structured message history
    summary: str
    error: str | None # To capture errors in the graph


# -----------------------------------------------------------------------------
# Generic LLM wrapper with Retry Logic
# -----------------------------------------------------------------------------
LLM = ChatOpenAI(model=CFG.model_name, temperature=CFG.temperature)

# Implement retry logic for LLM calls
@retry(
    stop=stop_after_attempt(3), # Retry up to 3 times
    wait=wait_fixed(2),         # Wait 2 seconds between retries
    reraise=True                # Re-raise the exception if all retries fail
)
def invoke_llm_with_retry(llm_client: ChatOpenAI, messages: List[BaseMessage]) -> str:
    """Invokes the LLM with retry logic for transient errors."""
    logger.debug("Invoking LLM with %d messages...", len(messages))
    try:
        # Optional: Track token usage and cost
        # with get_openai_callback() as cb:
        response = llm_client.invoke(messages)
            # logger.debug("LLM call stats: %s", cb)
        reply = response.content.strip()
        logger.debug("LLM reply received (first 100 chars): %s", reply[:100])
        return reply
    except APIError as e:
        logger.error("OpenAI API Error during LLM call: %s", e, exc_info=True)
        raise # Let retry handle it or fail
    except LangChainException as e:
        logger.error("LangChain Error during LLM call: %s", e, exc_info=True)
        raise # Let retry handle it or fail
    except Exception as e:
        logger.error("Unexpected error during LLM call: %s", e, exc_info=True)
        raise # Let retry handle it or fail

def ask_llm(prompt: str) -> str:
    """Synchronous LLM call using the retry wrapper."""
    logger.debug("ask_llm prompt (first 200 chars): %s", prompt[:200] + ("…" if len(prompt) > 200 else ""))
    # Use the global LLM client
    return invoke_llm_with_retry(LLM, [HumanMessage(content=prompt)])

# -----------------------------------------------------------------------------
# 1) Read & validate cluster JSON
# -----------------------------------------------------------------------------

def load_cluster_data(path: Path) -> Dict[str, dict]:
    """Loads cluster data from JSON, handling list or dict format."""
    logger.info("Loading cluster data from: %s", path)
    if not path.exists():
        logger.error("Cluster JSON file not found at %s", path)
        raise FileNotFoundError(f"Cluster JSON file not found: {path}")

    try:
        with path.open(encoding="utf-8") as f:
            loaded_data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Failed to decode JSON from %s: %s", path, e)
        raise ValueError(f"Invalid JSON file: {path}") from e
    except Exception as e:
        logger.error("Failed to read cluster file %s: %s", path, e)
        raise IOError(f"Could not read cluster file: {path}") from e

    clusters: Dict[str, dict]
    if isinstance(loaded_data, list):
        # Convert list to dict using string indices as keys.
        # This ensures downstream code using keys (like persona gen) works consistently.
        clusters = {str(i): item for i, item in enumerate(loaded_data)}
        logger.info("Loaded cluster data as list, converted to dict using string indices (0 to %d).", len(clusters) - 1)
    elif isinstance(loaded_data, dict):
        clusters = loaded_data
        logger.info("Loaded cluster data as dict.")
    else:
        logger.error("Unexpected data type loaded from JSON: %s. Expected dict or list.", type(loaded_data))
        raise TypeError(f"Unexpected data type in JSON file: {type(loaded_data)}")

    if not clusters:
        logger.error("Cluster data is empty after loading/processing.")
        raise ValueError("Cluster data is empty.")

    # Basic validation (optional but recommended)
    for key, value in clusters.items():
        if not isinstance(value, dict) or 'keywords' not in value or 'sentiment_dist' not in value:
            logger.warning("Cluster '%s' might be missing expected keys ('keywords', 'sentiment_dist').", key)

    logger.info("Successfully loaded and validated %d clusters.", len(clusters))
    return clusters


def pick_top_clusters(clusters: Dict[str, dict], k: int) -> Dict[str, dict]:
    """Selects top k clusters, ranked by negative sentiment count."""
    if k <= 0:
        return {}
    try:
        # Ensure robustness if sentiment_dist or negative key is missing
        ranked = sorted(
            clusters.items(),
            key=lambda kv: kv[1].get('sentiment_dist', {}).get("negative", 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )
    except Exception as e:
        logger.error("Error sorting clusters: %s. Check cluster data format.", e, exc_info=True)
        raise TypeError("Cluster data format seems incompatible for sorting.") from e

    num_to_select = min(k, len(ranked))
    if num_to_select < k:
        logger.warning("Requested %d clusters, but only %d available after ranking.", k, num_to_select)

    selected = dict(ranked[:num_to_select])
    logger.info("Selected top %d clusters for ideation: %s", len(selected), list(selected.keys()))
    return selected

# -----------------------------------------------------------------------------
# 2) Feature Ideation
# -----------------------------------------------------------------------------

def ideate_features(selected: Dict[str, dict], n: int = CFG.feature_count) -> List[FeatureProposal]:
    """Generates feature proposals based on selected clusters using an LLM."""
    logger.info("Starting feature ideation for %d features...", n)
    if not selected:
        logger.warning("No clusters selected for feature ideation.")
        return []

    cluster_details = []
    for cid, info in selected.items():
        # Basic validation of cluster info structure
        if not isinstance(info, dict):
            logger.warning("Skipping cluster '%s' in ideation prompt due to unexpected format: %s", cid, type(info))
            continue

        keywords = ', '.join(info.get('keywords', ['N/A']))
        neg_sentiment = info.get('sentiment_dist', {}).get('negative', 0)
        samples = info.get('samples', [])
        sample_feedback = samples[0] if samples else 'N/A'

        cluster_details.append(
            f"- **Cluster {cid}**:\n"
            f"  - Keywords: {keywords}\n"
            f"  - Negative Sentiment Count: {neg_sentiment}\n"
            f"  - Sample Feedback: '{sample_feedback[:200]}...'") # Limit sample length

    if not cluster_details:
        logger.error("No valid cluster details could be extracted for the feature ideation prompt.")
        return []

    cluster_str = "\n".join(cluster_details)

    prompt = (
        f"You are a Senior Product Manager at Spotify, specializing in user experience. "
        f"Your task is to propose exactly {n} concrete and realistic product features or UX improvements "
        f"that directly address the pain points highlighted in the following user feedback clusters. "
        f"Focus on actionable solutions that enhance user satisfaction.\n\n"
        f"**User Feedback Clusters:**\n"
        f"{cluster_str}\n\n"
        f"**Instructions:**\n"
        f"1. Generate exactly {n} distinct feature proposals.\n"
        f"2. Each proposal should be a clear, concise imperative statement (e.g., 'Implement a sleep timer', 'Improve playlist organization').\n"
        f"3. Ensure the features directly relate to the pain points identified in the cluster keywords and sample feedback.\n"
        f"4. Return EACH proposal on a new line, with NO prefixes (like numbers or dashes) and NO blank lines between proposals.\n\n"
        f"**Example Output Format:**\n"
        f"Allow collaborative playlist editing in real-time\n"
        f"Introduce higher fidelity audio options for subscribers\n"
        f"Simplify the podcast discovery interface"
    )
    # REMOVED print(prompt) - Rely on debug logging in ask_llm

    try:
        raw = ask_llm(prompt)
        # REMOVED print(raw) - Rely on debug logging in ask_llm

        # Process the raw response
        lines = [line.strip() for line in raw.split('\n') if line.strip()]
        # Filter out any potential introductory text or examples
        proposals_text = [line for line in lines if not line.startswith(("*", "-", "#")) and "|" not in line] # Basic filtering

        # Ensure we only take up to 'n' valid proposals
        proposals = [FeatureProposal(id=i + 1, description=desc) for i, desc in enumerate(proposals_text[:n])]

        if len(proposals) < n:
            logger.warning("LLM generated fewer than %d valid proposals. Got %d. Raw output:\n%s", n, len(proposals), raw)
        elif len(proposals) > n:
             logger.warning("LLM generated more than %d proposals. Taking the first %d. Raw output:\n%s", n, n, raw)
             proposals = proposals[:n] # Trim excess

        logger.info("Feature ideation complete. Generated %d features: %s", len(proposals), [p.description for p in proposals])
        return proposals

    except Exception as e:
        logger.error("Feature ideation failed: %s", e, exc_info=True)
        # Return empty list or re-raise depending on desired pipeline behavior
        return []


# -----------------------------------------------------------------------------
# 3) Persona Generation
# -----------------------------------------------------------------------------

def generate_personas(clusters: Dict[str, dict], count: int) -> List[Persona]:
    """
    Generates diverse user personas based on cluster data using a single LLM call
    expecting a JSON output.
    """
    if not clusters:
        logger.warning("No clusters provided for persona generation.")
        return []
    if count <= 0:
        logger.warning("Requested persona count is zero or negative.")
        return []

    # --- 1. Prepare Cluster Information ---
    cluster_details_list = []
    # Select up to 'count' clusters to base personas on, prioritizing as before if needed
    # (Using simple selection for this example, but could retain sorting)
    cluster_items = list(clusters.items())
    num_to_select = min(count, len(cluster_items)) # Aim to base personas on selected clusters

    if num_to_select < count:
        logger.warning(f"Requested {count} personas, but only {num_to_select} clusters available. Personas might be less diverse or draw inspiration from fewer clusters.")
    elif num_to_select == 0:
         logger.warning("No clusters available to generate personas from.")
         return []


    selected_clusters_for_prompt = cluster_items[:num_to_select]

    for cluster_id, cluster_data in selected_clusters_for_prompt:
        if not isinstance(cluster_data, dict):
            logger.warning(f"Skipping cluster '{cluster_id}' due to invalid format.")
            continue

        keywords_str = ", ".join(cluster_data.get('keywords', ['N/A']))
        sentiment_dist = cluster_data.get('sentiment_dist', {})
        samples = cluster_data.get('samples', [])
        sample_feedback = samples[0] if samples else 'N/A'
        sentiment_info = f"SentimentDist=[{', '.join(f'{k}: {v}' for k, v in sentiment_dist.items())}]"
        if 'avg_sentiment' in cluster_data:
             sentiment_info += f" | AvgSentiment={cluster_data['avg_sentiment']:.2f}"

        cluster_details_list.append(
            f'- Cluster {cluster_id}: Keywords=[{keywords_str}] | {sentiment_info} | Sample="{sample_feedback[:150]}..."'
        )

    if not cluster_details_list:
        logger.error("No valid cluster details could be extracted for persona generation prompt.")
        return []

    cluster_summary_str = "\\n".join(cluster_details_list)

    # --- 2. Construct the Prompt for JSON Output ---
    # Define the JSON structure expected using Persona fields
    json_format_example = """
    ```json
    [
      {
        "name": "Alex Chen",
        "background": "Deep, diverse description of the persona's background in 5-10 sentences",
        "quote": "Finding new music I actually like feels harder than it should be.",
        "sentiment": "neutral",
        "pain_points": [
          "Music discovery algorithm often misses the mark",
          "Too many ads in the free tier interrupt listening flow",
          "Playlist organization options are limited"
        ],
        "inspired_by_cluster_id": "3"
      },
      {
        "name": "Maria Garcia",
        "background": "Deep, diverse description of the persona's background in 5-10 sentences",
        "quote": "I just want to quickly find a good podcast for my drive or something safe for the kids.",
        "sentiment": "positive",
        "pain_points": [
          "Difficult to manage separate profiles effectively on family plan",
          "Podcast discovery feels cluttered",
          "Lack of robust parental control features"
        ],
        "inspired_by_cluster_id": "1"
      }
    ]
    ```
    """

    prompt = (
        f"You are an expert persona generator specializing in user experience research. Your task is to create exactly {count} distinct and deeply grounded Spotify user personas based on the provided user feedback cluster summaries.\n\n"
        f"**Requirements:**\n"
        f"1.  **Generate {count} Personas:** Create exactly this number.\n"
        f"2.  **Diversity:** Ensure personas have unique backgrounds, motivations, Spotify usage patterns, and personalities. Avoid stereotypes.\n"
        f"3.  **Grounded in Data:** Each persona's details (background, quote, sentiment, pain points) MUST directly reflect themes from the provided cluster summaries. Assign `inspired_by_cluster_id` to the cluster ID that most influenced the persona.\n"
        f"4.  **Sentiment:** Use ONLY 'positive', 'neutral', or 'negative' for the `sentiment` field.\n"
        f"5.  **Pain Points:** List specific, concrete frustrations or challenges the user faces with Spotify, derived from cluster keywords and feedback.\n"
        f"6.  **JSON Output:** Return ONLY a valid JSON list containing the persona objects. Do NOT include any explanatory text before or after the JSON block.\n"
        f"7.  **Format:** Your response MUST be a valid JSON array starting with [ and ending with ]. Each persona object must have all required fields.\n\n"
        f"**Cluster Summaries:**\n{cluster_summary_str}\n\n"
        f"**Required JSON Format Example:**\n{json_format_example}\n\n"
        f"Generate the JSON output now. Remember to:\n"
        f"- Start with [\n"
        f"- End with ]\n"
        f"- Include all required fields for each persona\n"
        f"- Do not add any text before or after the JSON\n"
        f"- Ensure the JSON is properly formatted and valid"
    )

    # --- 3. Call LLM and Parse JSON ---
    personas: List[Persona] = []
    try:
        raw_response = ask_llm(prompt)
        logger.debug("Raw LLM response for persona generation: %s", raw_response[:2500] + "...") # Log snippet

        # Clean the response by removing any markdown code fences and whitespace
        cleaned_response = raw_response.strip()
        if cleaned_response.startswith("```json"):
            cleaned_response = cleaned_response[7:]  # Remove ```json
        if cleaned_response.endswith("```"):
            cleaned_response = cleaned_response[:-3]  # Remove ```
        cleaned_response = cleaned_response.strip()

        # Basic validation of JSON structure
        if not cleaned_response.startswith("[") or not cleaned_response.endswith("]"):
            logger.error("LLM response does not start with [ or end with ]. Raw response: %s", cleaned_response[:200] + "...")
            return []

        try:
            parsed_json = json.loads(cleaned_response)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from LLM response. Error: %s. Response: %s", e, cleaned_response[:200] + "...")
            return []

        # --- 4. Validate and Instantiate Personas ---
        validated_count = 0
        for i, item in enumerate(parsed_json):
            if not isinstance(item, dict):
                logger.warning(f"Skipping item #{i+1} in JSON response: not a dictionary.")
                continue

            # Basic validation (add more checks as needed)
            required_keys = {"name", "background", "quote", "sentiment", "pain_points", "inspired_by_cluster_id"}
            if not required_keys.issubset(item.keys()):
                missing = required_keys - item.keys()
                logger.warning(f"Skipping persona #{i+1}: Missing required keys: {missing}. Data: {item}")
                continue

            sentiment = item.get("sentiment", "").lower()
            if sentiment not in ["positive", "neutral", "negative"]:
                logger.warning(f"Skipping persona '{item.get('name', 'Unknown')}': Invalid sentiment '{item.get('sentiment')}'.")
                continue

            pain_points = item.get("pain_points", [])
            if not isinstance(pain_points, list) or not all(isinstance(p, str) for p in pain_points):
                logger.warning(f"Skipping persona '{item.get('name', 'Unknown')}': Invalid 'pain_points' format (must be list of strings).")
                continue

            # Cluster ID can be None or string
            cluster_id = item.get("inspired_by_cluster_id")
            if cluster_id is not None and not isinstance(cluster_id, str):
                 # Try to convert if it's a number, otherwise warn
                 try:
                     cluster_id = str(cluster_id)
                 except:
                      logger.warning(f"Persona '{item.get('name', 'Unknown')}': Invalid 'inspired_by_cluster_id' format ({type(cluster_id)}). Setting to None.")
                      cluster_id = None


            try:
                personas.append(Persona(
                    name=str(item["name"]),
                    background=str(item["background"]),
                    quote=str(item["quote"]),
                    sentiment=sentiment,
                    pain_points=[str(p) for p in pain_points], # Ensure strings
                    inspired_by_cluster_id=cluster_id
                ))
                validated_count += 1
            except Exception as e: # Catch potential errors during instantiation
                logger.warning(f"Skipping persona '{item.get('name', 'Unknown')}' due to instantiation error: {e}. Data: {item}")
                continue

        logger.info(f"Successfully parsed and validated {validated_count} personas from LLM response.")

        # Check if count matches requested
        if validated_count < count:
             logger.warning(f"LLM generated fewer valid personas ({validated_count}) than requested ({count}).")
        elif validated_count > count:
             logger.warning(f"LLM generated more personas ({validated_count}) than requested ({count}). Truncating to {count}.")
             personas = personas[:count]


    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON from LLM response: {e}")
        logger.debug(f"Problematic JSON string: {cleaned_response}")
        # Optionally raise, or return empty/partial list
        return []
    except ValueError as e:
        logger.error(f"Validation error in parsed JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"Error during persona generation LLM call or processing: {e}", exc_info=True)
        return [] # Return empty list on failure

    # --- 5. Final Check and Return ---
    if not personas and count > 0:
         logger.error("Failed to generate any valid personas.")

    return personas

# -----------------------------------------------------------------------------
# 4) Board Simulation (Improved with Dynamic Facilitation & LLM Reuse)
# -----------------------------------------------------------------------------

def simulate_board(personas: Sequence[Persona],
                   features: Sequence[FeatureProposal],
                   rounds: int = 3
                   ) -> Tuple[str, List[BaseMessage]]:
    """Multi-round virtual board meeting using ConversationChain for each persona."""
    if not personas or not features:
        logger.warning("Missing personas (%d) or features (%d)", len(personas), len(features))
        return "", []

    logger.info("Initializing ConversationChains for %d personas...", len(personas))

    # Shared LLM for chains
    llm = LLM # Use the globally defined LLM

    # Define a conversational prompt template
    # Incorporates persona details, history, and current input
    convo_template = """
You are {persona_name}. Act and respond authentically based on this profile:
- Background: {persona_background}
- Overall Sentiment towards Spotify: {persona_sentiment}
- Key Pain Points/Frustrations: {persona_pain_points}

Always speak in the first person ('I', 'me', 'my'). Keep your responses concise (1-3 sentences unless asked otherwise) and focused on the discussion topic. Ground your opinions in your background and pain points. Be honest and natural. Avoid clichés like 'Honestly' or 'Thanks for bringing this up'.

Current Conversation:
{history}
Human: {input}
{persona_name}:""" # AI prefix matches persona name for clarity

    prompt = PromptTemplate(
        input_variables=["history", "input", "persona_name", "persona_background", "persona_sentiment", "persona_pain_points"],
        template=convo_template
    )

    # --- Initialize Chains ---
    chains = {}
    memories = {}
    for p in personas:
        # Create dedicated memory for each agent
        memory = ConversationBufferWindowMemory(
            k=5, # Keep last 5 interactions
            memory_key="history", # Matches prompt variable
            input_key="input" # Matches prompt variable
            # ai_prefix=p.name # Optional: match AI prefix in template
            # human_prefix="Human" # Default
        )
        memories[p.name] = memory

        # Create the ConversationChain
        chain = ConversationChain(
            llm=llm,
            prompt=prompt.partial( # Pre-fill persona details into the prompt
                persona_name=p.name,
                persona_background=p.background,
                persona_sentiment=p.sentiment,
                persona_pain_points="; ".join(p.pain_points)
            ),
            memory=memory,
            verbose=False # Set to True for debugging chain execution
        )
        chains[p.name] = chain
        logger.debug("Initialized ConversationChain for %s", p.name)

    # --- Simulation Setup ---
    feature_list_md = "\n".join(
        f"{i+1}. {f.description}" for i, f in enumerate(features))

    transcript: List[str] = [] # Initialize empty list
    # Overall history tracking (distinct from agent memory)
    global_history: List[BaseMessage] = []
    # Use description as the key for votes
    priority_votes: defaultdict[str, int] = defaultdict(int)

    # --- Simulation Loop ---
    for r in range(1, rounds + 1):
        logger.info("--- Starting Discussion Round %d ---", r)
        # ---- facilitator prompt ----
        if r == 1:
            fac_input = (f"Welcome, everyone. We have **{len(features)}** candidate features:\n"
                   f"{feature_list_md}\n\n"
                   "👉 In a SHORT paragraph: what excites or worries you most?")
        elif r == 2:
            fac_input = ("Thanks! Now dig deeper: for EACH feature name either one concrete risk "
                   "or one success metric. Any initial thoughts on potential impacts?")
        else:  # final
            fac_input = ("Time to prioritise. Pick ONE feature Spotify should ship next quarter & why "
                   ". Mention one trade-off you'd accept.")

        transcript += [f"\n### 🎤 Facilitator – Round {r}", fac_input]
        # Add facilitator turn to global history (important for context if not using shared memory)
        global_history.append(HumanMessage(content=fac_input))

        order = list(personas)
        random.shuffle(order)

        for p in order:
            logger.info("Simulating turn for persona %s in round %d", p.name, r)
            chain = chains[p.name]

            try:
                # Run the chain - it uses its internal memory for history
                reply = chain.predict(input=fac_input)
                reply = reply.strip() # Clean up whitespace

            except Exception as e:
                logger.error("ConversationChain execution failure for %s, round %d: %s", p.name, r, e, exc_info=True)
                reply = "(Persona encountered an error and could not generate response)"

            # Record persona's reply
            transcript += [f"\n#### 👤 {p.name}", reply]
            # Add agent's reply to global history
            global_history.append(AIMessage(content=reply))

            # Tally votes in the last round (using description)
            if r == rounds:
                for f in features:
                    if f.description.lower() in reply.lower():
                        priority_votes[f.description] += 1
                        logger.debug("Vote tallied for '%s' from %s", f.description, p.name)
                        break # Count first match only

    # transcript.append("```")   # Removed: Don't add markdown fence here
    final_transcript_md = "\n".join(transcript)
    logger.info("Board simulation done – %d messages in global history", len(global_history))

    # Return the formatted transcript and the global message history
    return final_transcript_md, global_history

# -----------------------------------------------------------------------------
# 5) Meeting Summary
# -----------------------------------------------------------------------------

def summarise_meeting(transcript_md: str, conversation_history: List[BaseMessage] | None = None) -> str:
    """Summarizes the virtual board meeting transcript using an LLM."""
    logger.info("Generating meeting summary...")
    if not transcript_md.strip():
        logger.warning("Transcript is empty, cannot generate summary.")
        return "Error: Transcript was empty."

    # Use the detailed prompt provided previously
    prompt = (
        "You are an expert meeting summarizer. Analyze the virtual user board meeting transcript provided below.\n"
        "Your summary MUST include these sections in markdown format:\n\n"
        "1.  **Pros & Cons per Feature:** For each proposed feature, list the key advantages (Pros) and disadvantages or concerns (Cons) raised by the participants. Be specific.\n"
        "2.  **Overall Sentiment & Key Takeaways per Persona:** Briefly describe each persona's overall stance, highlighting their main points, priorities, or key concerns.\n"
        "3.  **Points of Agreement & Disagreement:** Note any areas where personas strongly agreed or disagreed with each other.\n"
        "4.  **Final Recommendation:** Provide a concise (1-3 sentences) go/no-go/conditional recommendation for the features, explicitly mentioning the rationale based on the discussion (e.g., priority, concerns raised).\n\n"
        "---\n"
        f"{transcript_md}\n" # Embed the full transcript directly
        "---\n"
        "Generate the summary now."
    )

    try:
        summary = ask_llm(prompt)
        logger.info("Meeting summary generated successfully.")
        return summary
    except Exception as e:
        logger.error("Failed to generate meeting summary: %s", e, exc_info=True)
        return f"Error: Failed to generate meeting summary due to: {e}"


# -----------------------------------------------------------------------------
# 6) Report Writer (Simplified Transcript Handling)
# -----------------------------------------------------------------------------

def write_report(
    selected_clusters: Dict[str, dict],
    features: Sequence[FeatureProposal],
    personas: Sequence[Persona],
    transcript: str, # Expect pre-formatted Markdown transcript
    summary: str
):
    """Writes the final Markdown report to the output directory."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_path = CFG.output_dir / "board_session_report.md"
    logger.info("Writing final report to: %s", report_path)

    try:
        with report_path.open("w", encoding="utf-8") as f:
            # --- Header ---
            f.write("# 🎵 Spotify Virtual User-Board Session\n\n")
            f.write(f"*Generated on {ts}*\n\n")
            f.write("## 📊 Overview\n\n")
            f.write(f"- **Number of Features Discussed**: {len(features)}\n")
            f.write(f"- **Number of Personas**: {len(personas)}\n")
            f.write(f"- **Discussion Rounds**: {CFG.discussion_rounds}\n\n")

            # --- Selected Clusters ---
            f.write("## 🔍 Selected User Feedback Clusters\n\n")
            # Sort clusters by ID for consistent report order
            for cluster_id, cluster_data in sorted(selected_clusters.items(), key=lambda item: str(item[0])):
                f.write(f"### Cluster {cluster_id}\n\n")
                keywords = cluster_data.get('keywords', [])
                f.write(f"- **Keywords**: {', '.join(keywords)}\n")
                sentiment_dist = cluster_data.get('sentiment_dist', {})
                f.write("- **Sentiment Distribution**:\n")
                # Sort sentiment labels for consistent order
                for sentiment, count in sorted(sentiment_dist.items()):
                    f.write(f"  - {sentiment.capitalize()}: {count}\n")
                f.write("- **Sample Feedback**:\n")
                samples = cluster_data.get('samples', [])
                for sample in samples[:2]: # Show top 2 samples
                    # Clean sample for display (remove extra whitespace)
                    cleaned_sample = re.sub(r'\s+', ' ', str(sample)).strip()
                    f.write(f"  > {cleaned_sample}\n")
                f.write("\n")

            # --- Proposed Features ---
            f.write("## 💡 Proposed Features\n\n")
            if features:
                for feat in features:
                    f.write(f"### {feat.md()}\n\n")
            else:
                f.write("*No features were generated.*\n\n")

            # --- User Personas ---
            f.write("## 👥 User Personas\n\n")
            if personas:
                for persona in personas:
                    # Use the persona's built-in Markdown method
                    f.write(persona.md())
                    f.write("\n") # Add separator
            else:
                f.write("*No personas were generated.*\n\n")

            # --- Discussion Transcript (Write pre-formatted string directly) ---
            f.write("## 💬 Discussion Transcript\n\n")
            if transcript.strip():
                 # Wrap the transcript content in a markdown code block
                 f.write("```markdown\n")
                 f.write(transcript + "\n")
                 f.write("```\n\n")
            else:
                 f.write("*No discussion transcript was generated.*\n\n")


            # --- Meeting Summary ---
            f.write("## 📝 Meeting Summary\n\n")
            if summary.strip() and not summary.startswith("Error:"):
                 f.write(summary + "\n")
            else:
                 f.write(f"*Summary could not be generated. Details: {summary}*\n")


            # --- Footer ---
            f.write("\n---\n")
            f.write("*This report was generated using an AI-powered user board simulation pipeline.*\n")

        logger.info("Markdown report written successfully.")

    except IOError as e:
        logger.error("Failed to write report file %s: %s", report_path, e, exc_info=True)
    except Exception as e:
        logger.error("An unexpected error occurred during report writing: %s", e, exc_info=True)


# -----------------------------------------------------------------------------
# LANGGRAPH Orchestration – keeps each stage modular & observable
# -----------------------------------------------------------------------------

def build_pipeline():
    """Builds the LangGraph StateGraph pipeline."""
    logger.info("Building LangGraph pipeline...")
    graph = StateGraph(AgentState) # Use the typed state

    # Define node functions that update the state
    def run_feature_ideation(state: AgentState) -> Dict[str, Any]:
        try:
            features = ideate_features(state["selected_clusters"], CFG.feature_count)
            return {"features": features, "error": None}
        except Exception as e:
            logger.error("Error in feature ideation node: %s", e, exc_info=True)
            return {"error": f"Feature Ideation Failed: {e}"}

    def run_persona_generation(state: AgentState) -> Dict[str, Any]:
        if state.get("error"): return {} # Skip if previous step failed
        try:
            personas = generate_personas(state["selected_clusters"], CFG.persona_count)
            return {"personas": personas, "error": None}
        except Exception as e:
            logger.error("Error in persona generation node: %s", e, exc_info=True)
            return {"error": f"Persona Generation Failed: {e}"}

    def run_board_simulation(state: AgentState) -> Dict[str, Any]:
        if state.get("error"): return {} # Skip if previous step failed
        try:
            transcript_md, history = simulate_board(state["personas"], state["features"], CFG.discussion_rounds)
            return {"transcript": transcript_md, "conversation_history": history, "error": None}
        except Exception as e:
            logger.error("Error in board simulation node: %s", e, exc_info=True)
            return {"error": f"Board Simulation Failed: {e}"}

    def run_summary_generation(state: AgentState) -> Dict[str, Any]:
        if state.get("error"): return {} # Skip if previous step failed
        try:
            summary = summarise_meeting(state["transcript"], state["conversation_history"])
            return {"summary": summary, "error": None}
        except Exception as e:
            logger.error("Error in summary generation node: %s", e, exc_info=True)
            return {"error": f"Summary Generation Failed: {e}"}

    # Add nodes to the graph
    graph.add_node("ideate", run_feature_ideation)
    graph.add_node("generate_personas", run_persona_generation)
    graph.add_node("board", run_board_simulation)
    graph.add_node("generate_summary", run_summary_generation)

    # Define edges - standard sequential flow
    graph.set_entry_point("ideate")
    graph.add_edge("ideate", "generate_personas")
    graph.add_edge("generate_personas", "board")
    graph.add_edge("board", "generate_summary")
    graph.add_edge("generate_summary", END)

    # Compile the graph
    pipeline = graph.compile()
    logger.info("LangGraph pipeline compiled successfully.")
    return pipeline

# -----------------------------------------------------------------------------
# Main entrypoint
# -----------------------------------------------------------------------------

def main() -> None:
    """Main function to load data, build and run the pipeline, and write the report."""
    # Apply random seed early if specified
    if CFG.random_seed is not None:
         random.seed(CFG.random_seed)
         np.random.seed(CFG.random_seed) # Seed numpy as well
         # torch seeding can be added here if needed, see notes above
         logger.info("Using random seed: %d", CFG.random_seed)

    try:
        # --- Data Loading and Selection ---
        clusters = load_cluster_data(CFG.cluster_json)
        # Select clusters based on negative sentiment count for feature/persona generation
        selected_clusters_data = pick_top_clusters(clusters, k=CFG.persona_count)
        if not selected_clusters_data:
            logger.error("No clusters were selected. Cannot proceed.")
            sys.exit(1) # Exit if no clusters selected

        # --- Build and Run Pipeline ---
        pipeline = build_pipeline()

        # Define the initial state
        initial_state: AgentState = {
            "selected_clusters": selected_clusters_data,
            "features": [],
            "personas": [],
            "transcript": "",
            "conversation_history": [],
            "summary": "",
            "error": None # Initialize error state
        }

        logger.info("Invoking pipeline...")
        final_state = pipeline.invoke(initial_state)
        logger.info("Pipeline invocation complete.")

        # --- Error Check ---
        if final_state.get("error"):
            logger.error("Pipeline execution failed with error: %s", final_state["error"])
            # Still attempt to write a partial report if possible
            logger.warning("Attempting to write partial report despite pipeline error...")

        # --- Write Report ---
        # Ensure keys exist even if steps failed
        write_report(
            final_state.get("selected_clusters", selected_clusters_data), # Use initial if final missing
            final_state.get("features", []),
            final_state.get("personas", []),
            final_state.get("transcript", "*Transcript generation failed or skipped due to error.*"),
            final_state.get("summary", f"*Summary generation failed or skipped. Error: {final_state.get('error')}*")
        )

        if final_state.get("error"):
            logger.error("Pipeline finished with errors.")
            sys.exit(1) # Exit with error code
        else:
            logger.info("✅ Multi-agent pipeline finished successfully.")

    except FileNotFoundError as e:
        logger.error("❌ Pipeline failed: Required file not found.")
        logger.error(e, exc_info=True)
        sys.exit(2)
    except ValueError as e:
        logger.error("❌ Pipeline failed: Data validation or processing error.")
        logger.error(e, exc_info=True)
        sys.exit(3)
    except Exception as e:
        logger.error("❌ Pipeline failed with an unexpected error.")
        logger.error(e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()