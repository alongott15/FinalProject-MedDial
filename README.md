# ProjectMedDial 🩺

A comprehensive platform for simulating and evaluating medical dialogues, focusing on realism, safety, and adherence to medical standards. This project aims to enhance the quality and safety of AI-driven medical consultations by providing tools for dialogue generation, evaluation, and GTMF (Ground Truth Medical Form) creation. 🚀


## Table of Contents 📑

- [Project Title & Badges](#projectmeddial-)
- [Description](#projectmeddial-)
- [Table of Contents](#table-of-contents-)
- [Features](#features-)
- [Tech Stack](#tech-stack-)
- [Installation](#installation-)
- [Usage](#usage-)
- [Project Structure](#project-structure-)
- [API Reference](#api-reference-)
- [Contributing](#contributing-)
- [License](#license-)
- [Important Links](#important-links-)
- [Footer](#footer-)

## Features ✨

- **Dialogue Simulation**: Simulates doctor-patient conversations using sophisticated agent models. 🗣️
- **DeepEval-Based Evaluation**: Validates generated dialogues using deepeval GEval metrics (Naturalness, Patient Profile Compliance) and RAGAS Faithfulness scoring, all backed by Azure AI Foundry. ✅
- **Prompt Improvement Loop**: Automatically refines doctor and patient prompts based on judge feedback to iteratively improve dialogue quality. 🔄
- **Bias-Aware Generation**: Applies bias-aware prompts for dialogue summarization, EHR extraction, and GTMF creation to ensure fair and balanced outputs. ⚖️
- **GTMF (Ground Truth Medical Form) Creation**: Extracts and structures key medical information from clinical notes into GTMF markdown files using Azure AI. 📝
- **EHR Summarization**: Summarizes EHR/clinical notes using bias-aware prompts, extracting only information clearly present in the text. 🏥
- **Conversation Variety**: Ensures dialogue diversity through conversation variety utilities and repetition filtering. 🌿
- **MTS-Dialog Integration**: Loads and processes real-world medical conversation data from the MTS-Dialog dataset for grounding and evaluation. 📊
- **Progressive Disclosure**: Simulates gradual information disclosure from patients, supporting multiple profile types (FULL, NO_DIAGNOSIS, NO_DIAGNOSIS_NO_TREATMENT) for more realistic interactions.
- **Analysis & Visualization**: Provides tools to visualize results and framework diagrams for research and reporting. 📈

## Tech Stack 💻

- **Python**: Primary programming language. 🐍
- **Markdown**: Used for documentation and GTMF output. 📝
- **JSON**: Used for data serialization and configuration.
- **Transformers**: Utilized for advanced NLP tasks. 🧠
- **Torch**: Used for tensor computations.
- **Pydantic**: Used for data validation and settings management.
- **Azure AI Services**: Leveraged for language model functionalities and evaluations (GPT-4.1 via Azure AI Foundry). ☁️
- **deepeval**: Used for GEval metrics and RAGAS faithfulness evaluation. 🧪
- **Sentence Transformers**: Used for calculating sentence embeddings and semantic similarity.
- **Scikit-learn**: Utilized for machine learning tasks.
- **Pandas**: Used for CSV data loading and processing.
- **NLTK (Natural Language Toolkit)**: Used for text processing and tokenization.
- **dotenv**: Used for loading environment variables from a `.env` file.

## Installation 🛠️

1. **Clone the Repository**: ⬇️
   ```bash
   git clone https://github.com/alongott15/ProjectMedDial
   cd ProjectMedDial
   ```

2. **Create a Virtual Environment**: virtualenv is used in this project.
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Linux and macOS
   venv\Scripts\activate  # On Windows
   ```

3. **Install Dependencies**: ⚙️
   ```bash
   pip install -r requirements.txt
   ```
   Based on the `requirements.txt` file, the following packages are required:
   ```text
   transformers>=4.30.0
   torch>=2.0.0
   pydantic>=1.10.2
   scikit-learn>=1.2.0
   sentence-transformers>=2.2.0
   azure-ai-inference
   azure-core>=1.30.0
   python-dotenv
   nltk
   rouge-score
   pandas>=1.5.0
   deepeval>=0.21.0
   ```

4. **Configure Azure AI Credentials**: 🔑
   - Set the `AZURE_AI_ENDPOINT` and `AZURE_AI_API_KEY` environment variables. This can be done by creating a `.env` file in the project root:
     ```text
     AZURE_AI_ENDPOINT=your_azure_ai_endpoint
     AZURE_AI_API_KEY=your_azure_ai_api_key
     ```

## Usage 🚀

1. **Run the Dialogue Generation Framework**: 🗣️
   - Execute `dialogue_generation_framework.py` to generate medical dialogues with iterative quality improvement. The pipeline uses patient and doctor agents, evaluated by a DeepEval-based judge agent.
     ```bash
     python dialogue_generation_framework.py
     ```

2. **Create GTMFs from Clinical Notes**: 📝
   - Execute `gtmf_creation.py` to extract and structure Ground Truth Medical Forms from clinical notes into markdown files stored in the `gtmf/` directory.
     ```bash
     python gtmf_creation.py
     ```

3. **Run a Single Dialogue Simulation**: 🔬
   - Use `simulation.py` directly to simulate a single doctor-patient dialogue turn-by-turn.
     ```bash
     python simulation.py
     ```

### Key Use Cases:

- **Improving AI-driven Medical Consultations**: Validating and refining AI systems used in medical consultations by simulating dialogues and identifying areas for improvement.
- **Enhancing Medical Training**: Providing realistic scenarios for medical training and education, where students can practice communication and diagnostic skills.
- **Generating High-Quality Annotated Data**: Creating a dataset of realistic medical dialogues grounded in MTS-Dialog and MIMIC-III clinical notes for training and evaluating NLP models in the medical domain.
- **Bias-Aware Research**: Studying and mitigating demographic biases in AI-generated medical conversations.

### Example Scenario:

A researcher wants to create a dataset of high-quality medical dialogues. They first run `gtmf_creation.py` to extract structured medical forms from clinical notes, then use `dialogue_generation_framework.py` to generate dialogues grounded in those GTMFs. The framework automatically iterates and improves dialogue quality using the DeepEval judge until a quality threshold is met.

## Project Structure 📂

```
ProjectMedDial/
├── Agents/
│   ├── DeepEvalJudgeAgent.py       # Dialogue evaluation using deepeval GEval & RAGAS faithfulness
│   ├── DialogueSummarizerAgent.py  # Bias-aware dialogue summarization
│   ├── DoctorAgent.py              # Agent simulating a doctor
│   ├── EHRSummarizerAgent.py       # Bias-aware EHR/clinical note summarization
│   ├── JudgeAgent.py               # Legacy judge agent
│   ├── PatientAgent.py             # Agent simulating a patient
│   └── PromptImprovementAgent.py   # Refines agent prompts based on judge feedback
├── Models/
│   └── classes.py                  # Data classes for medical entities (GTMF, etc.)
├── Utils/
│   ├── bias_aware_prompts.py       # Bias-aware prompt templates for all agents
│   ├── conversation_variety.py     # Utilities for ensuring dialogue diversity
│   ├── csv_data_loader.py          # Loads and processes CSV datasets
│   ├── dialogue_markdown.py        # Saves dialogues in markdown format
│   ├── llms_utils.py               # Utility functions for interacting with LLMs
│   ├── markdown_gtmf.py            # GTMF markdown loading and saving utilities
│   ├── mts_dialog_loader.py        # Loader for the MTS-Dialog dataset
│   ├── partial_profile.py          # Functions for generating partial patient profiles
│   ├── repetition_filter.py        # Filters repetitive content in dialogues
│   └── utils.py                    # General utility functions
├── agent_prompts/
│   ├── BASE_SYSTEM_PROMPT.txt
│   ├── DialogueSummarizerAgent_PROMPT.txt
│   ├── DoctorAgent_PROMPT.txt
│   ├── EHRSummarizerAgent_PROMPT.txt
│   ├── JudgeAgent_PROMPT.txt
│   ├── PatientAgent_PROMPT.txt
│   └── PromptImprovementAgent_PROMPT.txt
├── MTS-Dialog/
│   └── MTS-Dialog.csv              # Real-world medical conversation dataset
├── analysis/
│   ├── figures/                    # Generated analysis figures
│   ├── framework_diagram.py        # Script for generating framework diagrams
│   └── visualize_results.py        # Script for visualizing evaluation results
├── gtmf/                           # Generated GTMF markdown files
├── output_dialogue_framework/      # Generated dialogue output files
├── dialogue_generation_framework.py # Main script for generating dialogues with iterative improvement
├── gtmf_creation.py                # Main script for creating GTMFs from clinical notes
├── simulation.py                   # Core dialogue simulation logic
├── README.md                       # This file
└── requirements.txt                # Project dependencies
```

## API Reference 📚

### `DeepEvalJudgeAgent`

- **`evaluate(dialogue: List[Dict], patient_profile: dict) -> dict`**: Evaluates a generated dialogue using deepeval GEval metrics and RAGAS faithfulness.
  - `dialogue` (List[Dict]): The dialogue turns to evaluate.
  - `patient_profile` (dict): The patient profile used to generate the dialogue.
  - Returns: A dictionary containing metric scores (naturalness, compliance, faithfulness) and an overall score. ✅

### `PromptImprovementAgent`

- **`improve(doctor_prompt: str, patient_prompt: str, judge_feedback: dict) -> dict`**: Refines agent prompts based on judge feedback.
  - `doctor_prompt` (str): The current doctor system prompt.
  - `patient_prompt` (str): The current patient system prompt.
  - `judge_feedback` (dict): Feedback from the DeepEvalJudgeAgent.
  - Returns: A dictionary with improved `doctor_prompt` and `patient_prompt`. 🔄

### `EHRSummarizerAgent`

- **`summarize(ehr_text: str) -> dict`**: Summarizes an EHR/clinical note using bias-aware prompts.
  - `ehr_text` (str): The clinical note text to summarize.
  - Returns: A structured dictionary of extracted medical information. 🏥

### `AzureAIFoundryClient` (via `llms_utils`)

- **`chat_generate(llm, messages: List[Dict[str, str]]) -> str`**: Generates chat completions using Azure AI Foundry.
  - `llm`: The Azure AI Foundry client instance.
  - `messages` (List[Dict[str, str]]): A list of message dictionaries with `role` and `content` keys.
  - Returns: The generated text content from the LLM. 💬

## Contributing 🤝

We welcome contributions to ProjectMedDial! To contribute:

1. Fork the repository. 🍴
2. Create a new branch for your feature or bug fix. 🌿
3. Implement your changes and add tests. ✅
4. Submit a pull request. 🚀

## License 📜

This project has no license. ⛔

## Important links 🔗

- Project Repository: [ProjectMedDial](https://github.com/alongott15/ProjectMedDial)

## Footer 🦶

© 2026 ProjectMedDial. Powered by [ProjectMedDial](https://github.com/alongott15/ProjectMedDial). Fork it, like it, raise issues and contribute to improve the project.


---
**<p align="center">Generated by [ReadmeCodeGen](https://www.readmecodegen.com/)</p>**
