"""Tests for profile command handlers."""

import argparse
import json
from unittest.mock import patch

import pytest

from browser_use.skill_cli.commands import profile as profile_commands


def test_discover_local_safari_profiles_parses_named_profiles():
	"""Safari profile discovery should expose direct profile labels from the File menu."""
	menu_items = json.dumps(
		[
			'New Window',
			'New Personal Window',
			'New Work Window',
			'New Private Window',
			'Close Window',
		]
	)

	with patch('browser_use.browser.backends.safari_backend._run_jxa_sync', return_value=menu_items):
		profiles, discovery_error = profile_commands._discover_local_safari_profiles()

	assert discovery_error is None
	assert profiles == [
		{'id': 'active', 'name': 'active', 'selection': 'Frontmost Safari window'},
		{'id': 'Personal', 'name': 'Personal', 'selection': 'File > New Personal Window'},
		{'id': 'Work', 'name': 'Work', 'selection': 'File > New Work Window'},
	]


def test_list_local_safari_profiles_uses_live_profile_language(capsys: pytest.CaptureFixture[str]):
	"""Safari profile list output should describe profile labels, not host bindings."""
	discovered = [
		{'id': 'active', 'name': 'active', 'selection': 'Frontmost Safari window'},
		{'id': 'Work', 'name': 'Work', 'selection': 'File > New Work Window'},
	]

	with patch.object(profile_commands, '_discover_local_safari_profiles', return_value=(discovered, None)):
		exit_code = profile_commands._list_local_safari_profiles(argparse.Namespace(json=False))

	assert exit_code == 0
	output = capsys.readouterr().out
	assert 'Safari profiles:' in output
	assert 'Frontmost Safari window' in output
	assert 'File > New Work Window' in output
	assert 'binding' not in output.lower()
	assert 'companion host' not in output.lower()


def test_get_local_safari_profile_guides_doctor_when_discovery_fails(capsys: pytest.CaptureFixture[str]):
	"""Profile lookup should point users to doctor when File menu discovery is unavailable."""
	with patch.object(
		profile_commands,
		'_discover_local_safari_profiles',
		return_value=([{'id': 'active', 'name': 'active', 'selection': 'Frontmost Safari window'}], 'access denied'),
	):
		exit_code = profile_commands._get_local_safari_profile(argparse.Namespace(id='Work', json=False))

	assert exit_code == 1
	error = capsys.readouterr().err
	assert 'Safari profile "Work" not found' in error
	assert 'File menu' in error
	assert 'doctor' in error
	assert 'companion host' not in error.lower()


@pytest.mark.parametrize(
	('handler', 'args', 'expected_lines'),
	[
		(
			profile_commands._handle_create,
			argparse.Namespace(name='Work', json=False),
			[
				'Error: Cannot create Safari profiles via CLI.',
				'Manage Safari profiles in Safari, then reuse the profile label in browser-use.',
				'Use --browser safari --profile <label> to target a Safari profile by name.',
			],
		),
		(
			profile_commands._handle_update,
			argparse.Namespace(id='Work', name='Renamed', json=False),
			[
				'Error: Cannot rename Safari profiles via CLI.',
				'Manage Safari profiles in Safari, then reuse the profile label in browser-use.',
				'--browser safari --profile <label>',
			],
		),
		(
			profile_commands._handle_delete,
			argparse.Namespace(id='Work', json=False),
			[
				'Error: Cannot delete Safari profiles via CLI.',
				'Manage Safari profiles in Safari, then reuse the profile label in browser-use.',
				'Delete the profile in Safari settings if you no longer want Browser Use to target it.',
			],
		),
		(
			profile_commands._handle_cookies,
			argparse.Namespace(id='Work', json=False),
			[
				'Error: Cookie listing is not available for Safari local profiles.',
				'Safari automation uses the live Safari app and does not expose raw cookie storage through this command.',
			],
		),
	],
)
def test_safari_profile_operations_use_local_backend_wording(
	handler,
	args: argparse.Namespace,
	expected_lines: list[str],
	capsys: pytest.CaptureFixture[str],
):
	"""Unsupported Safari operations should use local-backend wording without host references."""
	exit_code = handler(args, 'safari')

	assert exit_code == 1
	error = capsys.readouterr().err
	for line in expected_lines:
		assert line in error
	assert 'companion host' not in error.lower()
	assert 'binding' not in error.lower()


def test_print_usage_describes_safari_profiles(capsys: pytest.CaptureFixture[str]):
	"""Profile usage text should describe Safari profiles directly."""
	profile_commands._print_usage()

	output = capsys.readouterr().out
	assert 'Local Safari profiles' in output
	assert 'bindings' not in output.lower()
