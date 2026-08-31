"""Recover disclosure policy from dialogue text without case leakage (BENCH-5)."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


class DiscriminationError(ValueError):
    """The corpus cannot support a case-isolated policy classifier."""


@dataclass(frozen=True)
class DialoguePolicyRecord:
    case_id: str
    policy_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.case_id or not self.policy_id or not self.text.strip():
            raise DiscriminationError("case_id, policy_id and text are required")


@dataclass(frozen=True)
class PolicySplit:
    train: tuple[DialoguePolicyRecord, ...]
    test: tuple[DialoguePolicyRecord, ...]
    train_case_ids: frozenset[str]
    test_case_ids: frozenset[str]

    def __post_init__(self) -> None:
        overlap = self.train_case_ids & self.test_case_ids
        if overlap:
            raise DiscriminationError(
                f"case leakage across policy-classifier split: {sorted(overlap)}"
            )


@dataclass(frozen=True)
class DiscriminationReport:
    policies: tuple[str, ...]
    train_cases: tuple[str, ...]
    test_cases: tuple[str, ...]
    accuracy: float
    macro_auc: float
    top_features: dict[str, tuple[tuple[str, float], ...]]

    def as_record(self) -> dict[str, Any]:
        return {
            "policies": list(self.policies),
            "train_cases": list(self.train_cases),
            "test_cases": list(self.test_cases),
            "accuracy": self.accuracy,
            "macro_auc": self.macro_auc,
            "top_features": {
                policy: [
                    {"feature": feature, "weight": weight}
                    for feature, weight in features
                ]
                for policy, features in sorted(self.top_features.items())
            },
        }


def case_split(
    records: Sequence[DialoguePolicyRecord],
    *,
    test_fraction: float = 0.25,
    seed: int = 0,
) -> PolicySplit:
    """Put every policy arm belonging to a case on the same side of the split."""

    if not records:
        raise DiscriminationError("no policy-dialogue records were supplied")
    if not 0.0 < test_fraction < 1.0:
        raise DiscriminationError("test_fraction must be between zero and one")
    case_ids = sorted({record.case_id for record in records})
    if len(case_ids) < 2:
        raise DiscriminationError("at least two cases are required for a split")
    rng = random.Random(seed)
    shuffled = list(case_ids)
    rng.shuffle(shuffled)
    test_count = min(len(shuffled) - 1, max(1, round(len(shuffled) * test_fraction)))
    test_cases = frozenset(shuffled[:test_count])
    train_cases = frozenset(shuffled[test_count:])
    train = tuple(record for record in records if record.case_id in train_cases)
    test = tuple(record for record in records if record.case_id in test_cases)
    _require_policy_coverage(train, test)
    return PolicySplit(
        train=train,
        test=test,
        train_case_ids=train_cases,
        test_case_ids=test_cases,
    )


def evaluate_policy_discrimination(
    records: Sequence[DialoguePolicyRecord],
    *,
    test_fraction: float = 0.25,
    seed: int = 0,
    top_k: int = 10,
) -> DiscriminationReport:
    """Fit TF-IDF logistic regression and report held-out case AUC/features."""

    if top_k < 1:
        raise DiscriminationError("top_k must be positive")
    split = case_split(records, test_fraction=test_fraction, seed=seed)
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, roc_auc_score
        from sklearn.preprocessing import label_binarize
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise DiscriminationError("scikit-learn is required for BENCH-5") from exc

    train_text = [record.text for record in split.train]
    train_labels = [record.policy_id for record in split.train]
    test_text = [record.text for record in split.test]
    test_labels = [record.policy_id for record in split.test]
    policies = tuple(sorted(set(train_labels)))
    if len(policies) < 2:
        raise DiscriminationError("policy discrimination requires at least two policies")

    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        min_df=1,
        sublinear_tf=True,
    )
    train_matrix = vectorizer.fit_transform(train_text)
    test_matrix = vectorizer.transform(test_text)
    classifier = LogisticRegression(
        max_iter=2000,
        random_state=seed,
        solver="liblinear" if len(policies) == 2 else "lbfgs",
    )
    classifier.fit(train_matrix, train_labels)
    predictions = classifier.predict(test_matrix)
    probabilities = classifier.predict_proba(test_matrix)
    accuracy = float(accuracy_score(test_labels, predictions))
    if len(policies) == 2:
        positive = classifier.classes_[1]
        truth = [int(label == positive) for label in test_labels]
        macro_auc = float(roc_auc_score(truth, probabilities[:, 1]))
    else:
        truth = label_binarize(test_labels, classes=classifier.classes_)
        macro_auc = float(
            roc_auc_score(truth, probabilities, average="macro", multi_class="ovr")
        )

    feature_names = vectorizer.get_feature_names_out()
    coefficients = classifier.coef_
    if len(policies) == 2:
        # sklearn stores one binary coefficient vector for classes_[1]. The
        # negative of it gives equally inspectable drivers for classes_[0].
        coefficient_by_policy = {
            str(classifier.classes_[0]): -coefficients[0],
            str(classifier.classes_[1]): coefficients[0],
        }
    else:
        coefficient_by_policy = {
            str(policy): coefficients[index]
            for index, policy in enumerate(classifier.classes_)
        }
    top_features = {}
    for policy, weights in coefficient_by_policy.items():
        indices = sorted(
            range(len(feature_names)),
            key=lambda index: (-float(weights[index]), str(feature_names[index])),
        )[:top_k]
        top_features[policy] = tuple(
            (str(feature_names[index]), float(weights[index])) for index in indices
        )

    return DiscriminationReport(
        policies=tuple(str(policy) for policy in classifier.classes_),
        train_cases=tuple(sorted(split.train_case_ids)),
        test_cases=tuple(sorted(split.test_case_ids)),
        accuracy=accuracy,
        macro_auc=macro_auc,
        top_features=top_features,
    )


def _require_policy_coverage(
    train: Sequence[DialoguePolicyRecord], test: Sequence[DialoguePolicyRecord]
) -> None:
    all_policies = {record.policy_id for record in (*train, *test)}
    train_counts = Counter(record.policy_id for record in train)
    test_counts = Counter(record.policy_id for record in test)
    missing_train = all_policies - set(train_counts)
    missing_test = all_policies - set(test_counts)
    if missing_train or missing_test:
        raise DiscriminationError(
            "case split lacks policy coverage: "
            f"train missing {sorted(missing_train)}, test missing {sorted(missing_test)}"
        )
    # Repeated policy arms for one case would overweight it and usually mean
    # attempt records were used instead of final dialogues.
    by_case: dict[str, list[str]] = defaultdict(list)
    for record in (*train, *test):
        by_case[record.case_id].append(record.policy_id)
    duplicates = [
        case_id
        for case_id, labels in by_case.items()
        if len(labels) != len(set(labels))
    ]
    if duplicates:
        raise DiscriminationError(
            f"cases repeat a policy arm: {sorted(duplicates)}"
        )


__all__ = [
    "DialoguePolicyRecord",
    "DiscriminationError",
    "DiscriminationReport",
    "PolicySplit",
    "case_split",
    "evaluate_policy_discrimination",
]
