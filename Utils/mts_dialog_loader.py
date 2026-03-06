import pandas as pd
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RELEVANT_SECTIONS = [
    "CC",
    "GENHX",
    "ROS",
    "EXAM",
    "ASSESSMENT",
    "DIAGNOSIS",
    "PLAN"
]

def load_mts_dialog_examples(
    csv_path: str,
    sections: list[str] = None,
    max_examples: int = 5
) -> list[str]:
    if sections is None:
        sections = RELEVANT_SECTIONS

    csv_file = Path(csv_path)
    if not csv_file.exists():
        logger.warning(f"MTS-Dialog CSV not found at {csv_path}")
        return []

    try:
        df = pd.read_csv(csv_path)
        logger.info(f"Loaded MTS-Dialog dataset with {len(df)} rows")

        if 'section_header' not in df.columns or 'dialogue' not in df.columns:
            logger.warning("MTS-Dialog CSV missing required columns (section, dialogue)")
            return []

        filtered_df = df[df['section_header'].isin(sections)]
        logger.info(f"Filtered to {len(filtered_df)} relevant dialogues")

        examples = []
        for _, row in filtered_df.head(max_examples).iterrows():
            section = row.get('section_header', 'unknown')
            dialogue = row.get('dialogue', '')
            
            example = f"Section: {section}\n{dialogue}"
            examples.append(example)

        logger.info(f"Loaded {len(examples)} MTS-Dialog examples")
        return examples

    except Exception as e:
        logger.error(f"Error loading MTS-Dialog: {e}")
        return []


def format_mts_example(row: dict) -> str:
    section = row.get('section_header', 'unknown')
    dialogue = row.get('dialogue', '')
    return f"[MTS-Dialog Example - {section}]\n{dialogue}\n"