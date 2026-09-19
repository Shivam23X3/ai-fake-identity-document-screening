"""Document validation rule engine (Step 5).

Validates OCR-extracted document fields against configurable rule
definitions. Rules are grouped into per-document-type ``Ruleset``s that
can be registered per issuing country — adding a country or document
type means registering a new ruleset, never rewriting the engine.

IMPORTANT (honesty): the bundled rule sets are GENERIC, simplified
checks (formats, dates, MRZ check digits, expiry). They do NOT encode
any real country's full document rules. Country-specific rulesets are
an extension point, and every registry lookup is clearly-marked MOCK
data (see ``app/services/registry_service.py``).
"""
from app.services.validation.checks import ValidationCheck, ValidationOutcome
from app.services.validation.engine import ValidationEngine
from app.services.validation.rulesets import (
    Ruleset,
    RuleSpec,
    get_ruleset,
    register_ruleset,
)

__all__ = [
    "ValidationCheck",
    "ValidationOutcome",
    "ValidationEngine",
    "Ruleset",
    "RuleSpec",
    "get_ruleset",
    "register_ruleset",
]
