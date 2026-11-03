"""JSON schema validation. Every inter-agent document passes through here."""
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from . import config


class SchemaError(ValueError):
    def __init__(self, name, errors):
        self.schema_name = name
        self.errors = errors
        super().__init__(f"{name}: " + "; ".join(errors))


def schemas_dir() -> Path:
    # Schemas ship with the code, so use the package location, not AGENTSYS_HOME.
    return Path(__file__).resolve().parent.parent / "schemas"


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    path = schemas_dir() / f"{name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def errors_for(name: str, doc) -> list:
    out = []
    for e in sorted(_validator(name).iter_errors(doc), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in e.path) or "<root>"
        out.append(f"{loc}: {e.message}")
    return out


def validate(name: str, doc) -> None:
    errs = errors_for(name, doc)
    if errs:
        raise SchemaError(name, errs)


def validate_file(name: str, path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(name, doc)
    return doc
