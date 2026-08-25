from meddial.knowledge import mask_profile_for_patient


def generate_partial_profiles(
    full_profile: dict, profile_type: str = "NO_DIAGNOSIS_NO_TREATMENT"
) -> dict:
    """Compatibility wrapper around the formal patient knowledge policy."""
    return mask_profile_for_patient(full_profile, profile_type)


def generate_all_profile_types(full_profile: dict) -> list:
    return [
        generate_partial_profiles(full_profile, "FULL"),
        generate_partial_profiles(full_profile, "NO_DIAGNOSIS"),
        generate_partial_profiles(full_profile, "NO_DIAGNOSIS_NO_TREATMENT")
    ]
