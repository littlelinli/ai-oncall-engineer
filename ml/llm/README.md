# Дообучение языковой модели Kezekshi

Lambda `kezekshi-llm` пишет «Вероятную причину» инцидента. Базовая модель Qwen2.5-1.5B-Instruct
дообучается на нашей задаче методом QLoRA, и в систему ставится уже наша модель.

1. `make_dataset.py`: датасет (факты инцидента → объяснение причины, RU и EN). Факты из сценариев
   Kezekshi, ответы из формулировок команды. Настоящие примеры добавляются через `--extra`.
2. `finetune_qlora.ipynb`: обучение в Google Colab на бесплатном GPU T4, сравнение «базовая против
   дообученной» на отложенных 10%, сохранение в GGUF.
   Открыть: https://colab.research.google.com/github/littlelinli/ai-oncall-engineer/blob/main/ml/llm/finetune_qlora.ipynb
3. Подключение: файл `.gguf` в S3 `kezekshi-dataset-<account>/models/llm/`, в `terraform/envs/dev.tfvars`
   строка `llm_model_key = "models/llm/<файл>.gguf"`, затем `terraform apply`.

Метрики сравнения: доля ответов, прошедших проверки Lambda (язык, длина, нет выдуманных чисел),
доля ответов с правильной причиной, chrF к эталону, средняя длина.
