"""
Illustrative example: how RSF calls Claude with a JSON-schema-enforced
structured output, instead of parsing free-text model responses.

This is NOT the production prompt or the production schema. The real
system prompt encodes the actual evaluation methodology (the specific
weighting, exclusion rules, and scoring rubric behind Truth Score, Bucket,
Strategic Adjustment, and Desire Score) and is intentionally excluded from
this repository -- see the README's "What's not included" section for why.

What this file DOES accurately represent:
  - the general shape of the four output measures (simplified to a handful
    of fields here; the real schema has more structure per field)
  - the pattern of enforcing that shape via the Messages API's structured
    output support, rather than trusting the model to format text output
    consistently
  - computing the final letter grade server-side, deterministically, from
    the model's own numeric outputs -- never asking the model to do that
    arithmetic itself, which would be neither reliable nor auditable

A simplified system prompt stands in for the real one, just enough to show
how the four-layer model (see README) maps onto the request.
"""

import json
import urllib.request

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Illustrative only -- roughly 5% the length of, and materially less
# specific than, the real system prompt.
EXAMPLE_SYSTEM_PROMPT = """\
You evaluate a job opportunity for one specific candidate. Produce four
independent measures that must not contaminate one another:

- truth_score (0-100): pure capability fit. Can this candidate actually do
  the job? Never adjust this for compensation, location, or other personal
  preferences -- those belong in strategic_adjustment instead.
- bucket: how an employer would perceive this candidate in a fast resume
  scan, independent of whether the candidate wants the role.
- strategic_adjustment (a bounded adjustment in either direction): how much
  the candidate's own career strategy and preferences should raise or lower
  their interest, on top of raw capability.
- recommendation: a one-sentence pursue/avoid call weighing all of the above.
"""

# Illustrative only -- a representative subset of the real schema's shape,
# not the full field set or its exact constraints.
EXAMPLE_SCHEMA = {
    "type": "object",
    "properties": {
        "truth_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "bucket": {
            "type": "string",
            "enum": ["Strong Match", "Good Match", "Possible Match", "Weak Match"],
        },
        "strategic_adjustment": {
            "type": "object",
            "properties": {
                # Bounded in production; the specific numeric range is
                # intentionally not reproduced here.
                "value": {"type": "integer"},
                "factors": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["value", "factors"],
        },
        "recommendation": {"type": "string"},
    },
    "required": ["truth_score", "bucket", "strategic_adjustment", "recommendation"],
    "additionalProperties": False,
}


def score_opportunity(api_key, candidate_summary, job_description):
    """Call Claude with the schema enforced, then compute the derived score
    server-side rather than trusting the model to do the arithmetic."""
    body = {
        "model": "claude-opus-4-8",
        "max_tokens": 4000,
        "system": EXAMPLE_SYSTEM_PROMPT,
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": EXAMPLE_SCHEMA},
        },
        "messages": [{
            "role": "user",
            "content": f"Candidate:\n{candidate_summary}\n\nJob description:\n{job_description}",
        }],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    result = json.loads(payload["content"][0]["text"])

    # The model never computes the final score itself -- the server does,
    # deterministically, so the arithmetic is always correct and auditable.
    desire_score = max(0, min(100, result["truth_score"] + result["strategic_adjustment"]["value"]))
    result["desire_score"] = desire_score
    result["grade"] = _letter_grade(desire_score)
    return result


def _letter_grade(score):
    for lo, grade in [(90, "A"), (80, "B"), (70, "C"), (60, "D")]:
        if score >= lo:
            return grade
    return "F"
