# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the OpenAlex settings loader (#51, spec §5.5 Step 2)."""
from __future__ import annotations

import pytest

from factlog.integrations.openalex.config import (
    API_MAX_PER_PAGE,
    DEFAULT_LIMIT,
    OpenAlexConfig,
    OpenAlexConfigError,
    default_config_paths,
    from_mapping,
    load_config,
    xdg_config_path,
)


def write(path, text: str):
    path.write_text(text, encoding="utf-8")
    return path


class TestFromMapping:
    def test_empty_mapping_yields_defaults(self):
        cfg = from_mapping({})
        assert cfg == OpenAlexConfig()
        assert cfg.email == ""
        assert cfg.default_limit == DEFAULT_LIMIT
        assert cfg.max_limit == API_MAX_PER_PAGE

    def test_reads_client_and_import_sections(self):
        cfg = from_mapping({
            "client": {"email": "  user@example.com  "},
            "import": {"default_limit": 50, "max_limit": 100,
                       "default_target": "~/wiki", "skip_duplicates": False,
                       "include_abstract": False},
        })
        assert cfg.email == "user@example.com"
        assert cfg.default_limit == 50
        assert cfg.max_limit == 100
        assert cfg.default_target == "~/wiki"
        assert cfg.skip_duplicates is False
        assert cfg.include_abstract is False

    def test_non_string_email_fails_loud(self):
        with pytest.raises(OpenAlexConfigError, match="email must be a string"):
            from_mapping({"client": {"email": 42}})

    def test_wrong_typed_values_fall_back_to_defaults(self):
        cfg = from_mapping({"import": {"default_limit": "many", "skip_duplicates": "yes",
                                       "default_target": 7}})
        assert cfg.default_limit == DEFAULT_LIMIT
        assert cfg.skip_duplicates is True
        assert cfg.default_target == ""

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_not_read_as_a_limit(self, value):
        # bool subclasses int; `default_limit = true` must not mean per_page=1.
        assert from_mapping({"import": {"default_limit": value}}).default_limit == DEFAULT_LIMIT

    @pytest.mark.parametrize("value", [0, -1, 201, 10_000])
    def test_out_of_range_limits_fall_back(self, value):
        cfg = from_mapping({"import": {"default_limit": value, "max_limit": value}})
        assert cfg.default_limit == DEFAULT_LIMIT
        assert cfg.max_limit == API_MAX_PER_PAGE

    def test_default_limit_is_clamped_to_max_limit(self):
        # Otherwise every un-flagged search would fail at the API.
        cfg = from_mapping({"import": {"default_limit": 100, "max_limit": 10}})
        assert cfg.max_limit == 10
        assert cfg.default_limit == 10

    def test_non_dict_sections_are_ignored(self):
        assert from_mapping({"client": "nope", "import": []}) == OpenAlexConfig()


class TestLoadConfig:
    def test_explicit_path_is_read(self, tmp_path):
        path = write(tmp_path / "openalex.toml",
                     '[client]\nemail = "a@b.c"\n\n[import]\ndefault_limit = 5\n')
        cfg = load_config(path)
        assert cfg.email == "a@b.c"
        assert cfg.default_limit == 5

    def test_missing_explicit_path_is_an_error(self, tmp_path):
        with pytest.raises(OpenAlexConfigError, match="openalex config not found"):
            load_config(tmp_path / "absent.toml")

    def test_malformed_toml_is_an_error(self, tmp_path):
        path = write(tmp_path / "bad.toml", "[client\nemail =")
        with pytest.raises(OpenAlexConfigError, match="invalid TOML"):
            load_config(path)

    def test_no_config_anywhere_yields_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert load_config(kb_root=tmp_path / "kb") == OpenAlexConfig()

    def test_kb_policy_file_wins_over_user_file(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        write(xdg / "factlog" / "openalex.toml", '[client]\nemail = "user@example.com"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        write(kb / "policy" / "openalex-config.toml", '[client]\nemail = "kb@example.com"\n')

        assert load_config(kb_root=kb).email == "kb@example.com"

    def test_user_file_is_used_when_kb_has_no_policy(self, tmp_path, monkeypatch):
        xdg = tmp_path / "xdg"
        (xdg / "factlog").mkdir(parents=True)
        write(xdg / "factlog" / "openalex.toml", '[client]\nemail = "user@example.com"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        assert load_config(kb_root=tmp_path / "kb").email == "user@example.com"

    def test_email_is_honored_from_a_kb_policy_file(self, tmp_path):
        # Unlike Zotero's web_api_key, `email` is not a credential: OpenAlex is
        # unauthenticated, so a KB-scoped file may legitimately set it.
        kb = tmp_path / "kb"
        (kb / "policy").mkdir(parents=True)
        write(kb / "policy" / "openalex-config.toml", '[client]\nemail = "kb@example.com"\n')
        assert load_config(kb_root=kb).email == "kb@example.com"


class TestPaths:
    def test_xdg_path_honors_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert xdg_config_path() == tmp_path / "factlog" / "openalex.toml"

    def test_xdg_path_defaults_under_home(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert xdg_config_path().parts[-3:] == (".config", "factlog", "openalex.toml")

    def test_search_order_puts_kb_policy_first(self, tmp_path):
        paths = default_config_paths(tmp_path)
        assert paths[0] == tmp_path / "policy" / "openalex-config.toml"
        assert paths[-1] == xdg_config_path()

    def test_without_kb_root_only_the_user_file_is_searched(self):
        assert default_config_paths() == [xdg_config_path()]
