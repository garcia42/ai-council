import unittest

import council_tools.git_process_contract as git_process_contract
import council_tools.git_transport_identity as git_transport_identity
from council_tools.git_process_contract import (
    BASE_CHILD_ENVIRONMENT,
    DEFAULT_EXPLICIT_ENVIRONMENT_KEYS,
    MAX_ARGUMENT_LENGTH,
)
from council_tools.git_transport_identity import (
    PERMITTED_SCHEMES,
    TRANSPORT_ENVIRONMENT_KEYS,
    GitTransportIdentityError,
    RemoteTarget,
    transport_child_environment,
    transport_policy,
    validate_remote_url,
)


class PolicySetTest(unittest.TestCase):
    """Both sets are pinned exactly. Widening either is a policy change."""

    def test_the_transport_key_set_is_exactly_these_two(self):
        self.assertEqual(
            TRANSPORT_ENVIRONMENT_KEYS, frozenset({"SSH_AUTH_SOCK", "GIT_SSH_COMMAND"})
        )

    def test_the_scheme_allow_list_is_exactly_these_three(self):
        self.assertEqual(PERMITTED_SCHEMES, frozenset({"file", "ssh", "https"}))

    def test_no_credential_helper_proxy_or_trace_key_is_permitted(self):
        for key in (
            "GIT_ASKPASS",
            "GIT_CREDENTIAL_HELPER",
            "GIT_PROXY_COMMAND",
            "GIT_TRACE",
            "GIT_TRACE_PACKET",
            "ALL_PROXY",
            "HTTPS_PROXY",
            "SSH_ASKPASS",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, TRANSPORT_ENVIRONMENT_KEYS)

    def test_unauthenticated_schemes_are_absent(self):
        for scheme in ("git", "http", "ftp", "rsync"):
            with self.subTest(scheme=scheme):
                self.assertNotIn(scheme, PERMITTED_SCHEMES)

    def test_the_policy_is_the_default_widened_by_exactly_the_transport_keys(self):
        self.assertEqual(
            transport_policy().explicit_keys,
            frozenset(DEFAULT_EXPLICIT_ENVIRONMENT_KEYS | TRANSPORT_ENVIRONMENT_KEYS),
        )

    def test_the_process_contract_default_is_not_widened(self):
        """The policy extends the set; it must not edit the module that owns it."""
        self.assertEqual(
            git_process_contract.DEFAULT_EXPLICIT_ENVIRONMENT_KEYS,
            frozenset(
                {
                    "GIT_AUTHOR_NAME",
                    "GIT_AUTHOR_EMAIL",
                    "GIT_AUTHOR_DATE",
                    "GIT_COMMITTER_NAME",
                    "GIT_COMMITTER_EMAIL",
                    "GIT_COMMITTER_DATE",
                }
            ),
        )

    def test_no_transport_key_shadows_the_base_environment(self):
        for key in TRANSPORT_ENVIRONMENT_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(key, BASE_CHILD_ENVIRONMENT)


class EnvironmentTest(unittest.TestCase):
    def test_the_base_suppression_set_survives_intact(self):
        environment = transport_child_environment()
        for key, value in BASE_CHILD_ENVIRONMENT.items():
            with self.subTest(key=key):
                self.assertEqual(environment[key], value)

    def test_a_permitted_transport_key_is_carried(self):
        environment = transport_child_environment({"SSH_AUTH_SOCK": "/run/agent.sock"})
        self.assertEqual(environment["SSH_AUTH_SOCK"], "/run/agent.sock")

    def test_both_permitted_transport_keys_are_carried(self):
        environment = transport_child_environment(
            {"SSH_AUTH_SOCK": "/run/a.sock", "GIT_SSH_COMMAND": "ssh -F /dev/null"}
        )
        self.assertEqual(environment["GIT_SSH_COMMAND"], "ssh -F /dev/null")

    def test_an_authorship_key_is_still_permitted(self):
        environment = transport_child_environment({"GIT_AUTHOR_NAME": "council"})
        self.assertEqual(environment["GIT_AUTHOR_NAME"], "council")

    def test_an_unpermitted_transport_key_is_refused(self):
        for key in ("GIT_PROXY_COMMAND", "GIT_TRACE", "SSH_ASKPASS", "PATH"):
            with self.subTest(key=key):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    transport_child_environment({key: "x"})
                self.assertEqual(caught.exception.code, "transport-key-not-permitted")
                self.assertEqual(caught.exception.detail, key)

    def test_a_base_key_cannot_be_overridden(self):
        with self.assertRaises(GitTransportIdentityError) as caught:
            transport_child_environment({"GIT_TERMINAL_PROMPT": "1"})
        self.assertEqual(caught.exception.code, "transport-key-not-permitted")

    def test_nothing_from_the_ambient_environment_appears(self):
        import os

        environment = transport_child_environment()
        permitted = set(BASE_CHILD_ENVIRONMENT)
        for key in environment:
            with self.subTest(key=key):
                self.assertIn(key, permitted)
        for key in ("PATH", "USER", "SSH_AUTH_SOCK", "HOSTNAME", "PWD"):
            if key in os.environ and key not in BASE_CHILD_ENVIRONMENT:
                with self.subTest(ambient=key):
                    self.assertNotIn(key, environment)

    def test_the_returned_mapping_does_not_alias_the_caller(self):
        explicit = {"SSH_AUTH_SOCK": "/run/a.sock"}
        environment = transport_child_environment(explicit)
        environment["SSH_AUTH_SOCK"] = "/run/other.sock"
        self.assertEqual(explicit["SSH_AUTH_SOCK"], "/run/a.sock")

    def test_two_calls_do_not_share_a_mapping(self):
        first = transport_child_environment()
        first["LC_ALL"] = "en_US.UTF-8"
        self.assertEqual(transport_child_environment()["LC_ALL"], "C")

    def test_a_read_only_mapping_is_accepted(self):
        """The process contract accepts only a dict, so a Mapping that is not one
        must be converted here rather than reaching it and being refused."""
        from types import MappingProxyType

        environment = transport_child_environment(
            MappingProxyType({"SSH_AUTH_SOCK": "/run/a.sock"})
        )
        self.assertEqual(environment["SSH_AUTH_SOCK"], "/run/a.sock")

    def test_a_non_mapping_environment_is_refused(self):
        for value in ([], "SSH_AUTH_SOCK=x", 1):
            with self.subTest(value=value):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    transport_child_environment(value)
                self.assertEqual(caught.exception.code, "invalid-type")


class RemoteUrlTest(unittest.TestCase):
    def test_each_permitted_scheme_is_accepted(self):
        for url, scheme in (
            ("file:///tmp/claim.git", "file"),
            ("ssh://git.example.org/claim.git", "ssh"),
            ("https://git.example.org/claim.git", "https"),
        ):
            with self.subTest(url=url):
                target = validate_remote_url(url)
                self.assertIsInstance(target, RemoteTarget)
                self.assertEqual(target.scheme, scheme)
                self.assertEqual(target.url, url)

    def test_scheme_matching_is_case_insensitive(self):
        self.assertEqual(validate_remote_url("SSH://h/x").scheme, "ssh")

    def test_an_unpermitted_scheme_is_refused_and_named(self):
        for url, scheme in (
            ("git://h/x", "git"),
            ("http://h/x", "http"),
            ("ftp://h/x", "ftp"),
            ("rsync://h/x", "rsync"),
        ):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(url)
                self.assertEqual(caught.exception.code, "scheme-not-permitted")
                self.assertEqual(caught.exception.detail, scheme)

    def test_a_remote_with_no_scheme_at_all_is_refused(self):
        """urlsplit yields an empty scheme once an '@' or a '/' precedes the colon."""
        for url in (
            "git@github.com:garcia42/ai-council.git",
            "user@host:22/path",
            "./relative",
            "/abs/path",
        ):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(url)
                self.assertEqual(caught.exception.code, "missing-scheme")

    def test_an_scp_like_remote_is_refused_even_when_it_parses_as_a_scheme(self):
        """`host:path` parses as scheme `host`; a scheme allow-list is not enough."""
        for url in ("host:path", "h:p", "example.org:claim.git"):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(url)
                self.assertEqual(caught.exception.code, "scp-like-remote")

    def test_a_permitted_scheme_without_the_authority_form_is_refused(self):
        """Measured on git 2.39.5: `git ls-remote file:remote.git` does not read a
        file URL. It reads scp-like syntax and tries SSH to a host named 'file'
        ("ssh: Could not resolve hostname file"), while urlsplit reads scheme
        'file' with a relative path. Accepting it would validate one remote while
        git reached another."""
        for url in ("file:remote.git", "ssh:host/path", "https:host/path"):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(url)
                self.assertEqual(caught.exception.code, "scp-like-remote")

    def test_the_scp_like_refusal_precedes_the_scheme_refusal(self):
        """A remote that is not a URL at all is the more fundamental refusal."""
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url("git:path")
        self.assertEqual(caught.exception.code, "scp-like-remote")
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url("git://h/x")
        self.assertEqual(caught.exception.code, "scheme-not-permitted")

    def test_userinfo_is_refused_in_every_permitted_scheme(self):
        """Measured: git ls-remote 'file://user:pass@<path>' exits 0 and lists
        the ref, so a URL carrying a credential works while putting the secret
        in argv."""
        for url in (
            "file://user:pass@/tmp/claim.git",
            "ssh://git@git.example.org/claim.git",
            "ssh://git:token@git.example.org/claim.git",
            "https://token@git.example.org/claim.git",
            "https://user:token@git.example.org/claim.git",
        ):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(url)
                self.assertEqual(caught.exception.code, "url-carries-userinfo")

    def test_userinfo_is_refused_rather_than_stripped(self):
        with self.assertRaises(GitTransportIdentityError):
            validate_remote_url("https://token@h/x")
        # and the bare form is accepted, so the refusal is about the userinfo
        self.assertEqual(validate_remote_url("https://h/x").url, "https://h/x")

    def test_userinfo_is_refused_before_an_empty_host_is_reported(self):
        """A URL carrying a credential must be refused for carrying it."""
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url("https://token@/x")
        self.assertEqual(caught.exception.code, "url-carries-userinfo")

    def test_an_empty_host_is_refused_for_a_host_bearing_scheme(self):
        for url in ("ssh:///x", "https:///x"):
            with self.subTest(url=url):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(url)
                self.assertEqual(caught.exception.code, "empty-host")

    def test_a_file_url_needs_a_path_and_no_host(self):
        self.assertEqual(validate_remote_url("file:///tmp/x").scheme, "file")
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url("file://")
        self.assertEqual(caught.exception.code, "empty-path")

    def test_a_relative_file_path_cannot_reach_validation(self):
        """It would resolve against a cwd policy does not control."""
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url("file:claim.git")
        self.assertEqual(caught.exception.code, "scp-like-remote")

    def test_non_text_urls_are_refused(self):
        for value in (None, 1, b"file:///tmp/x", ["file:///tmp/x"]):
            with self.subTest(value=value):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(value)
                self.assertEqual(caught.exception.code, "invalid-type")

    def test_blank_and_padded_urls_are_refused(self):
        for value in ("", " https://h/x", "https://h/x ", "  "):
            with self.subTest(value=value):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url(value)
                self.assertEqual(caught.exception.code, "non-canonical-url")

    def test_a_control_character_is_refused(self):
        for suffix in ("\n", "\r", "\x00", "\x1b[31m", "\x7f"):
            with self.subTest(suffix=repr(suffix)):
                with self.assertRaises(GitTransportIdentityError) as caught:
                    validate_remote_url("https://h/x" + suffix + "y")
                self.assertEqual(caught.exception.code, "control-character")

    def test_a_url_one_character_over_the_bound_is_refused(self):
        """One over, not far over: a far-over case survives an off-by-one."""
        prefix = "https://h/"
        url = prefix + "x" * (MAX_ARGUMENT_LENGTH + 1 - len(prefix))
        self.assertEqual(len(url), MAX_ARGUMENT_LENGTH + 1)
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url(url)
        self.assertEqual(caught.exception.code, "url-too-long")

    def test_a_far_over_length_url_is_refused(self):
        url = "https://h/" + "x" * MAX_ARGUMENT_LENGTH
        with self.assertRaises(GitTransportIdentityError) as caught:
            validate_remote_url(url)
        self.assertEqual(caught.exception.code, "url-too-long")

    def test_a_url_exactly_at_the_bound_is_accepted(self):
        prefix = "https://h/"
        url = prefix + "x" * (MAX_ARGUMENT_LENGTH - len(prefix))
        self.assertEqual(len(url), MAX_ARGUMENT_LENGTH)
        self.assertEqual(validate_remote_url(url).scheme, "https")

    def test_the_target_is_frozen(self):
        import dataclasses

        target = validate_remote_url("https://h/x")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            target.url = "https://elsewhere/x"

    def test_error_is_a_value_error_with_a_stable_code_and_field(self):
        with self.assertRaises(ValueError) as caught:
            validate_remote_url("git://h/x")
        self.assertEqual(caught.exception.code, "scheme-not-permitted")
        self.assertEqual(caught.exception.field, "url")


class NoConnectionTest(unittest.TestCase):
    """This module returns values. It must not be able to reach anything."""

    def test_the_module_imports_no_process_socket_or_network_machinery(self):
        import pathlib

        source = pathlib.Path(git_transport_identity.__file__).read_text(encoding="utf-8")
        body = "\n".join(
            line
            for line in source.splitlines()
            if not line.lstrip().startswith(("#", "*"))
        )
        for forbidden in (
            "import subprocess",
            "import socket",
            "import http",
            "import urllib.request",
            "os.system",
            "Popen",
            "check_output",
            "urlopen",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)


if __name__ == "__main__":
    unittest.main()
