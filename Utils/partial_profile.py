def generate_partial_profiles(full_profile: dict, profile_type: str = "NO_DIAGNOSIS_NO_TREATMENT") -> dict:
    import copy

    if profile_type == "FULL":
        profile = copy.deepcopy(full_profile)
        profile["profile_type"] = "FULL"
        return profile

    elif profile_type == "NO_DIAGNOSIS":
        profile = copy.deepcopy(full_profile)

        if "Core_Fields" in profile and "Diagnoses" in profile["Core_Fields"]:
            profile["Core_Fields"]["Diagnoses"] = []

        profile["profile_type"] = "NO_DIAGNOSIS"
        return profile

    else:
        profile = copy.deepcopy(full_profile)

        if "Core_Fields" in profile:
            profile["Core_Fields"]["Diagnoses"] = []
            profile["Core_Fields"]["Treatment_Options"] = []

        profile["profile_type"] = "NO_DIAGNOSIS_NO_TREATMENT"
        return profile


def generate_all_profile_types(full_profile: dict) -> list:
    return [
        generate_partial_profiles(full_profile, "FULL"),
        generate_partial_profiles(full_profile, "NO_DIAGNOSIS"),
        generate_partial_profiles(full_profile, "NO_DIAGNOSIS_NO_TREATMENT")
    ]