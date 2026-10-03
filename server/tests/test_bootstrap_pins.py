"""The G2 pin-verdict contract (2026-10-14 handoff, recommendation YES):

For ollama-store imports, the catalog's `revision` is the manifest digest,
and only a digest-shape pin can be checked. A pin that is absent, empty,
or NOT digest- shaped is "pin unknown": bootstrap must complete the import
and record the digest (the runner prints it loudly with the catalog line
to pin), not refuse. A digest-shaped pin that mismatches keeps refusing
(fail-closed), and a wrong pin's failure stays AFTER the download
(staging preserved) exactly as today.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

import modal_service

DIGEST = "2f87bab55b736f739e3f687f28025b7b15955508356b305964126051223f9e2e"


class TestPinVerdict:
    def test_digest_shaped_matching_pin_verifies(self):
        assert modal_service._pin_verdict(DIGEST, f"sha-{DIGEST}") == "verified"

    def test_digest_shaped_mismatching_pin_refuses(self):
        other = "f" * 64
        assert modal_service._pin_verdict(DIGEST, f"sha-{other}") == "refuse"

    def test_refuse_reports_both_values(self):
        other = "f" * 64
        verdict = modal_service._pin_verdict(DIGEST, f"sha-{other}")
        assert verdict == "refuse"
        # the refuse message carries both values (the discovery mechanism):
        message = modal_service._pin_message(
            "humanizer", "hf.co/r/humanizer-GGUF:Q8_0", verdict, f"sha-{other}", DIGEST
        )
        assert DIGEST in message
        assert other in message

    @pytest.mark.parametrize("unknown", ["", "0" * 40, "head", "00000", "not-a-digest"])
    def test_non_digest_shaped_pin_is_pin_unknown(self, unknown):
        assert modal_service._pin_verdict(unknown, f"sha-{DIGEST}") == "unknown"

    def test_pin_unknown_completes_with_loud_digest_line(self):
        line = modal_service._pin_message(
            "humanizer", "hf.co/r/humanizer-GGUF:Q8_0", "unknown", f"sha-{DIGEST}", DIGEST
        )
        # the exact replace line a runner copies for the catalog pin:
        assert '"revision":' in line
        assert DIGEST in line
        assert "unknown" in line

    def test_digest_prefix_normalization(self):
        # api/tags' digest may carry a sha- (or sha256:) prefix; the verdict
        # normalizes before comparing:
        assert modal_service._pin_verdict(DIGEST, f"sha256:{DIGEST}") == "verified"
