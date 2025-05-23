def analyze_summarization_methods():
    """Analyze and compare extractive and abstractive summarization methods."""
    return {
        "extractive_method": {
            "advantages": [
                "показывает конкретные факты и детали из оригинального текста",
                "помогает более точно передать информацию изначального текста"
            ],
            "disadvantages": [
                "ограничен в выражении авторского стиля и тона",
                "может упускать нюансы и индивидуальные толкования текста"
            ]
        },
        "abstractive_method": {
            "advantages": [
                "позволяет выразить общее содержание текста в новой форме",
                "создает более креативный и литературный подход к информации"
            ],
            "disadvantages": [
                "может вносить субъективные интерпретации",
                "требует большего понимания контекста для успешного создания резюме"
            ]
        },
        "comparison": {
            "key_differences": [
                "Экстрактивный метод представляет факты и детали из текста, в то время как абстрактивный метод создает обобщенное представление текста",
                "Экстрактивный метод более прямолинеен, в то время как абстрактивный метод более творческий"
            ],
            "use_cases": {
                "extractive_better_for": [
                    "когда требуется точное и детальное изложение текста",
                    "для обучения и анализа исследовательских статей"
                ],
                "abstractive_better_for": [
                    "для творческого обобщения информации",
                    "для литературных или репортажных целей"
                ]
            }
        }
    } 