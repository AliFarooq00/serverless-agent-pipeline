"""Validate statemachine/*.asl.json with cfn-lint's Step Functions schema (rule E3601).

cfn-lint only inspects *inline* definitions, and SAM's DefinitionUri keeps ours in a file.
This wraps each definition in a throwaway template so CI gets the same schema validation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    status = 0
    for path in sorted((ROOT / "statemachine").glob("*.asl.json")):
        definition = json.loads(path.read_text())
        subs = {
            k: f"arn:aws:lambda:us-east-1:123456789012:function:{k}"
            for k in set(re.findall(r"\$\{(\w+)\}", path.read_text()))
        }
        template = {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "StateMachine": {
                    "Type": "AWS::StepFunctions::StateMachine",
                    "Properties": {
                        "RoleArn": "arn:aws:iam::123456789012:role/lint",
                        "Definition": definition,
                        "DefinitionSubstitutions": subs,
                    },
                }
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(template, fh)
        result = subprocess.run(["cfn-lint", fh.name], capture_output=True, text=True)  # noqa: S603,S607
        Path(fh.name).unlink()
        print(f"{path.relative_to(ROOT)}: {'ok' if result.returncode == 0 else 'FAILED'}")
        if result.returncode:
            print(result.stdout or result.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
