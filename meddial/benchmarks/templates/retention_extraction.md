---SYSTEM---
Extract only clinical facts explicitly stated in the dialogue. Do not infer a
diagnosis, medication, dose, or treatment from symptoms. Return exactly one
JSON object with this schema:
{"diagnoses": ["string"], "medications": ["string"]}

Use empty arrays when the dialogue states no item in a field. Do not include
explanation or Markdown.
---USER---
Dialogue (and no other clinical context):
{transcript}
