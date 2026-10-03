"""Smoke tests for the Modal inference service application constants."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import config


class TestInferenceConfig:
    def test_default_app_name(self):
        assert config.APP_NAME == "modal-inference-server"

    def test_default_gpu(self):
        assert config.GPU == "H100"

    def test_models_file(self):
        assert config.MODELS_FILE == "models.json"

    def test_scaledown_window_default(self):
        assert config.SCALEDOWN_WINDOW == 300

    def test_max_concurrent_default(self):
        assert config.MAX_CONCURRENT_INPUTS == 8

    def test_export_limit_default(self):
        assert config.EXPORT_LIMIT_MAX == 500

    def test_auth_secret_name(self):
        assert config.AUTH_SECRET_NAME == "inference-auth-secret"
