import os
import logging
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

def format_date(dt, fmt: str) -> str:
    if pd.isna(dt) or dt is None:
        return 'Not provided'
    if hasattr(dt, 'strftime'):
        return dt.strftime(fmt)
    dt_str = str(dt)
    try:
        parsed = pd.to_datetime(dt_str)
        return parsed.strftime(fmt)
    except:
        return dt_str

def calculate_age(birth_date_str: str, admission_date_str: str) -> int:
    try:
        if not birth_date_str or birth_date_str == 'Not provided':
            return -1
        if not admission_date_str or admission_date_str == 'Not provided':
            return -1

        birth_date = pd.to_datetime(birth_date_str)
        admission_date = pd.to_datetime(admission_date_str)

        age = admission_date.year - birth_date.year
        if (admission_date.month, admission_date.day) < (birth_date.month, birth_date.day):
            age -= 1

        return age
    except Exception as e:
        logger.error(f"Error calculating age: {e} (Birth: {birth_date_str}, Admission: {admission_date_str})")
        return -1
