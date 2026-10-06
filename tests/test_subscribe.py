"""Tests for the subscribe command group."""

import gzip
import json
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import click
import pytest

from korgalore import PublicInboxError, RemoteError
from korgalore.cli import (
    find_subscription_file,
    generate_subscription_config,
)
from korgalore.lei_feed import LeiFeed
from korgalore.lore_feed import LoreFeed
from tests.digest_helpers import make_ctx


class TestValidatePublicInboxUrl:
    """Tests for LoreFeed.validate_public_inbox_url static method."""

    @staticmethod
    def _make_manifest_response(manifest_data: Dict[str, Any]) -> MagicMock:
        """Create a mock response with gzipped manifest JSON."""
        json_bytes = json.dumps(manifest_data).encode()
        compressed = gzip.compress(json_bytes)
        response = MagicMock()
        response.content = compressed
        response.raise_for_status = MagicMock()
        return response

    @staticmethod
    def _make_mock_node(response: MagicMock) -> MagicMock:
        """Create a mock LoreNode whose request() returns the given response."""
        mock_node = MagicMock()
        mock_node.request.return_value = response
        mock_node.close = MagicMock()
        return mock_node

    @pytest.mark.parametrize(
        ('url', 'manifest', 'expected'),
        [
            (
                'https://lore.kernel.org/lkml/',
                {'/lkml/git/0.git': {'fingerprint': 'abc123'}, '/lkml/git/1.git': {'fingerprint': 'def456'}},
                'lkml',
            ),
            # Works with any public-inbox server, not just lore
            ('https://inbox.example.org/mylist/', {'/mylist/git/0.git': {'fingerprint': 'abc'}}, 'mylist'),
        ],
        ids=['lore-two-epochs', 'non-lore-server'],
    )
    def test_success(self, url: str, manifest: Dict[str, Any], expected: str) -> None:
        """Valid manifest with consistent list prefix returns list name."""
        mock_node = self._make_mock_node(self._make_manifest_response(manifest))

        with patch('korgalore.make_lore_node', return_value=mock_node):
            result = LoreFeed.validate_public_inbox_url(url)

        assert result == expected
        mock_node.request.assert_called_once_with('GET', f'{url}manifest.js.gz')
        mock_node.close.assert_called_once()

    def test_mixed_prefixes_raises_remote_error(self) -> None:
        """Manifest with inconsistent list prefixes raises RemoteError."""
        manifest = {
            '/lkml/git/0.git': {'fingerprint': 'abc'},
            '/other/git/0.git': {'fingerprint': 'def'},
        }
        response = self._make_manifest_response(manifest)
        mock_node = self._make_mock_node(response)

        with patch('korgalore.make_lore_node', return_value=mock_node):
            with pytest.raises(RemoteError, match='inconsistent list prefixes'):
                LoreFeed.validate_public_inbox_url('https://lore.kernel.org/lkml/')

    def test_fetch_failure_raises_remote_error(self) -> None:
        """Failed manifest fetch raises RemoteError."""
        mock_node = MagicMock()
        mock_node.request.side_effect = Exception('Connection refused')
        mock_node.close = MagicMock()

        with patch('korgalore.make_lore_node', return_value=mock_node):
            with pytest.raises(RemoteError, match='Failed to fetch manifest'):
                LoreFeed.validate_public_inbox_url('https://lore.kernel.org/lkml/')

    def test_empty_manifest_raises_remote_error(self) -> None:
        """Empty manifest raises RemoteError."""
        response = self._make_manifest_response({})
        mock_node = self._make_mock_node(response)

        with patch('korgalore.make_lore_node', return_value=mock_node):
            with pytest.raises(RemoteError, match='Empty manifest'):
                LoreFeed.validate_public_inbox_url('https://lore.kernel.org/lkml/')


class TestValidateLeiPath:
    """Tests for LeiFeed.validate_lei_path static method."""

    def test_success(self, tmp_path: Path) -> None:
        """Valid lei v2 search path returns the path."""
        lei_path = tmp_path / 'lei' / 'my-search'
        lei_path.mkdir(parents=True)

        ls_data = [
            {'output': f'v2:{lei_path}'},
            {'output': 'v2:/some/other/path'},
        ]
        output = json.dumps(ls_data).encode()

        with patch('korgalore.lei_feed.run_lei_command', return_value=(0, output)):
            result = LeiFeed.validate_lei_path(str(lei_path))

        assert result == str(lei_path)

    def test_not_found_raises_public_inbox_error(self, tmp_path: Path) -> None:
        """Unknown lei path raises PublicInboxError."""
        lei_path = tmp_path / 'lei' / 'nonexistent'

        ls_data = [
            {'output': 'v2:/some/other/path'},
        ]
        output = json.dumps(ls_data).encode()

        with patch('korgalore.lei_feed.run_lei_command', return_value=(0, output)):
            with pytest.raises(PublicInboxError, match='not found as a v2 lei search'):
                LeiFeed.validate_lei_path(str(lei_path))

    def test_lei_command_failure_raises_public_inbox_error(self) -> None:
        """Failed lei ls-search raises PublicInboxError."""
        with patch('korgalore.lei_feed.run_lei_command', return_value=(1, b'error')):
            with pytest.raises(PublicInboxError, match='LEI list searches failed'):
                LeiFeed.validate_lei_path('/some/path')


class TestGenerateSubscriptionConfig:
    """Tests for generate_subscription_config function."""

    def test_generates_valid_toml(self) -> None:
        """Generated config is valid TOML, with a labels array and metadata comments."""
        content = generate_subscription_config(
            feed_key='lkml',
            url='https://lore.kernel.org/lkml/',
            target='personal',
            labels=['INBOX', 'UNREAD', 'IMPORTANT'],
        )

        config = tomllib.loads(content)

        assert 'lkml' in config['feeds']
        assert config['feeds']['lkml']['url'] == 'https://lore.kernel.org/lkml/'
        assert 'lkml' in config['deliveries']
        assert config['deliveries']['lkml']['feed'] == 'lkml'
        assert config['deliveries']['lkml']['target'] == 'personal'
        assert config['deliveries']['lkml']['labels'] == ['INBOX', 'UNREAD', 'IMPORTANT']
        assert "labels = ['INBOX', 'UNREAD', 'IMPORTANT']" in content
        assert '# Auto-generated by: kgl subscribe add' in content
        assert '# Generated:' in content

    def test_lei_path_gets_prefix(self) -> None:
        """Non-URL path gets lei: prefix."""
        content = generate_subscription_config(
            feed_key='my-search',
            url='/home/user/lei/my-search',
            target='maildir',
            labels=['INBOX'],
        )

        config = tomllib.loads(content)
        assert config['feeds']['my-search']['url'] == 'lei:/home/user/lei/my-search'


@pytest.mark.parametrize(
    ('present', 'expected'),
    [
        (['sub-lkml.toml'], 'sub-lkml.toml'),
        (['sub-lkml.toml.paused'], 'sub-lkml.toml.paused'),
        (['sub-lkml.toml', 'sub-lkml.toml.paused'], 'sub-lkml.toml'),
        ([], None),
    ],
    ids=['active', 'paused', 'prefers-active-over-paused', 'not-found'],
)
def test_find_subscription_file(tmp_path: Path, present: List[str], expected: Optional[str]) -> None:
    conf_d = tmp_path / 'conf.d'
    conf_d.mkdir()
    for name in present:
        (conf_d / name).write_text('[feeds.lkml]\n')

    result = find_subscription_file(conf_d, 'lkml')
    assert result == (conf_d / expected if expected else None)


class TestDefaultCommandGroup:
    """Tests for DefaultCommandGroup falling back to 'add'."""

    def test_url_without_add_subcommand(self) -> None:
        """subscribe <url> is treated as subscribe add <url>."""
        from korgalore.cli import DefaultCommandGroup

        group = DefaultCommandGroup(name='subscribe')

        @group.command('add')
        @click.argument('url')
        def add_cmd(url: str) -> None:
            pass

        @group.command('list')
        def list_cmd() -> None:
            pass

        # A known command resolves normally
        cmd_name, _cmd, args = group.resolve_command(click.Context(group), ['list'])
        assert cmd_name == 'list'

        # An unknown token falls back to 'add'
        cmd_name, _cmd, args = group.resolve_command(click.Context(group), ['https://lore.kernel.org/lkml/'])
        assert cmd_name == 'add'
        assert args == ['https://lore.kernel.org/lkml/']


def sub_ctx(
    tmp_path: Path,
    subs: Optional[Dict[str, str]] = None,
    **obj: Any,
) -> click.Context:
    """A context for the subscribe commands.

    subs maps conf.d file names to their contents; conf.d is only created when
    subs is not None. Extra keyword arguments are added to ctx.obj. The conf.d
    path is tmp_path / 'config' / 'conf.d' and data_dir is tmp_path / 'data'.
    """
    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    cfgpath = config_dir / 'korgalore.toml'
    cfgpath.write_text('')
    if subs is not None:
        conf_d = config_dir / 'conf.d'
        conf_d.mkdir()
        for fname, content in subs.items():
            (conf_d / fname).write_text(content)
    return make_ctx({'cfgpath': cfgpath, 'data_dir': tmp_path / 'data', **obj})


def conf_d_of(tmp_path: Path) -> Path:
    return tmp_path / 'config' / 'conf.d'


STUB = '[feeds.lkml]\n'


class TestSubscribeAdd:
    """Tests for the subscribe add command."""

    @staticmethod
    def _add_ctx(
        tmp_path: Path,
        targets: Dict[str, Any],
        subs: Optional[Dict[str, str]] = None,
        feeds: Optional[Dict[str, Any]] = None,
        deliveries: Optional[Dict[str, Any]] = None,
    ) -> click.Context:
        return sub_ctx(
            tmp_path,
            subs,
            config={'targets': targets, 'feeds': feeds or {}, 'deliveries': deliveries or {}},
            targets={},
            feeds={},
            deliveries={},
        )

    @pytest.mark.parametrize(
        ('targets', 'target', 'labels', 'expected_target', 'expected_labels'),
        [
            ({'gmail': {'type': 'gmail'}}, 'gmail', (), 'gmail', ['INBOX', 'UNREAD']),
            # Auto-selects the target when only one is configured
            ({'only-target': {'type': 'gmail'}}, None, (), 'only-target', ['INBOX', 'UNREAD']),
            ({'gmail': {'type': 'gmail'}}, 'gmail', ('CUSTOM', 'LABELS'), 'gmail', ['CUSTOM', 'LABELS']),
        ],
        ids=['lore-default-labels', 'auto-selects-single-target', 'custom-labels'],
    )
    def test_add_lore(
        self,
        tmp_path: Path,
        targets: Dict[str, Any],
        target: Optional[str],
        labels: Tuple[str, ...],
        expected_target: str,
        expected_labels: List[str],
    ) -> None:
        """subscribe add creates a conf.d file for a lore URL."""
        from korgalore.cli import subscribe_add

        ctx = self._add_ctx(tmp_path, targets)
        mock_target = MagicMock()
        mock_target.DEFAULT_LABELS = ['INBOX', 'UNREAD']

        with (
            patch.object(LoreFeed, 'validate_public_inbox_url', return_value='lkml'),
            patch('korgalore.cli.get_target', return_value=mock_target),
        ):
            ctx.invoke(subscribe_add, url='https://lore.kernel.org/lkml/', target=target, labels=labels)

        config_file = conf_d_of(tmp_path) / 'sub-lkml.toml'
        assert config_file.exists()

        config = tomllib.loads(config_file.read_text())
        assert 'lkml' in config['feeds']
        assert config['deliveries']['lkml']['target'] == expected_target
        assert config['deliveries']['lkml']['labels'] == expected_labels

    def test_add_lei(self, tmp_path: Path) -> None:
        """subscribe add creates conf.d file for lei path."""
        from korgalore.cli import subscribe_add

        lei_path = '/home/user/lei/my-search'
        ctx = self._add_ctx(tmp_path, {'maildir': {'type': 'maildir'}})
        mock_target = MagicMock()
        mock_target.DEFAULT_LABELS = ['INBOX']

        with (
            patch.object(LeiFeed, 'validate_lei_path', return_value=lei_path),
            patch('korgalore.cli.get_target', return_value=mock_target),
        ):
            ctx.invoke(subscribe_add, url=lei_path, target='maildir', labels=())

        # Lei paths use the directory basename as feed key
        config_file = conf_d_of(tmp_path) / 'sub-my-search.toml'
        assert config_file.exists()

        config = tomllib.loads(config_file.read_text())
        assert 'my-search' in config['feeds']
        assert config['feeds']['my-search']['url'] == f'lei:{lei_path}'

    @pytest.mark.parametrize(
        ('key', 'target', 'subs', 'feeds', 'deliveries'),
        [
            ('lkml', 'gmail', {'sub-lkml.toml': STUB}, None, None),
            ('lkml', 'gmail', None, {'lkml': {'url': 'https://lore.kernel.org/lkml/'}}, None),
            (
                'kernelnewbies',
                'fastmail',
                None,
                None,
                {
                    'kernelnewbies': {
                        'feed': 'https://lore.kernel.org/kernelnewbies',
                        'target': 'fastmail',
                        'labels': ['kernelnewbies'],
                    }
                },
            ),
        ],
        ids=['conf-d-file', 'feed-in-main-config', 'delivery-in-main-config'],
    )
    def test_add_duplicate_aborts(
        self,
        tmp_path: Path,
        key: str,
        target: str,
        subs: Optional[Dict[str, str]],
        feeds: Optional[Dict[str, Any]],
        deliveries: Optional[Dict[str, Any]],
    ) -> None:
        """subscribe add aborts when the key already exists in conf.d or main config."""
        from korgalore.cli import subscribe_add

        ctx = self._add_ctx(tmp_path, {target: {'type': target}}, subs, feeds, deliveries)

        with patch.object(LoreFeed, 'validate_public_inbox_url', return_value=key):
            with pytest.raises(click.exceptions.Abort):
                ctx.invoke(subscribe_add, url=f'https://lore.kernel.org/{key}/', target=target, labels=())


class TestSubscribeList:
    """Tests for the subscribe list command."""

    @staticmethod
    def _ctx(tmp_path: Path, subs: Dict[str, str]) -> click.Context:
        """A context whose conf.d holds the given sub-*.toml[.paused] files."""
        contents = {
            fname: generate_subscription_config(key, f'https://lore.kernel.org/{key}/', 'gmail', ['INBOX'])
            for fname, key in subs.items()
        }
        return sub_ctx(tmp_path, contents if subs else None)

    BOTH = {'sub-lkml.toml': 'lkml', 'sub-netdev.toml.paused': 'netdev'}

    @pytest.mark.parametrize(
        ('subs', 'paused', 'expected', 'absent'),
        [
            (BOTH, False, ['  lkml\n', '  netdev [paused]\n', 'URL: https://lore.kernel.org/netdev/'], []),
            (BOTH, True, ['  netdev [paused]\n'], ['lkml']),
            ({'sub-lkml.toml': 'lkml'}, True, ['No paused subscriptions.'], ['lkml']),
            ({}, False, ['No subscriptions found.'], []),
        ],
        ids=['active-and-paused', 'paused-only', 'nothing-paused', 'no-conf-d'],
    )
    def test_list(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        subs: Dict[str, str],
        paused: bool,
        expected: list[str],
        absent: list[str],
    ) -> None:
        from korgalore.cli import subscribe_list

        ctx = self._ctx(tmp_path, subs)
        with caplog.at_level('INFO', logger='korgalore'):
            ctx.invoke(subscribe_list, paused=paused)
        text = caplog.text
        for line in expected:
            assert line in text
        for word in absent:
            assert word not in text


class TestSubscribePauseResume:
    """Tests for subscribe pause and resume commands."""

    @pytest.mark.parametrize('start', ['sub-lkml.toml', 'sub-lkml.toml.paused'], ids=['active', 'already-paused'])
    def test_pause(self, tmp_path: Path, start: str) -> None:
        """subscribe pause renames .toml to .toml.paused; an already-paused one just warns."""
        from korgalore.cli import subscribe_pause

        ctx = sub_ctx(tmp_path, {start: STUB})

        ctx.invoke(subscribe_pause, feed_key='lkml')

        assert sorted(p.name for p in conf_d_of(tmp_path).iterdir()) == ['sub-lkml.toml.paused']

    @pytest.mark.parametrize('start', ['sub-lkml.toml.paused', 'sub-lkml.toml'], ids=['paused', 'already-active'])
    def test_resume(self, tmp_path: Path, start: str) -> None:
        """subscribe resume renames .toml.paused to .toml; an already-active one just warns."""
        from korgalore.cli import subscribe_resume

        ctx = sub_ctx(tmp_path, {start: STUB})

        ctx.invoke(subscribe_resume, feed_key='lkml', skip=False)

        assert sorted(p.name for p in conf_d_of(tmp_path).iterdir()) == ['sub-lkml.toml']

    def test_resume_skip(self, tmp_path: Path) -> None:
        """subscribe resume --skip deletes delivery info files.

        On the next pull, update_all_feeds() clones any new epochs first, then
        load_delivery_info() auto-creates state from the current highest epoch
        tip, so the info files must go while the epoch clones stay.
        """
        from korgalore.cli import subscribe_resume

        ctx = sub_ctx(tmp_path, {'sub-lkml.toml.paused': STUB})

        # Feed data directory with delivery info files
        feed_dir = tmp_path / 'data' / 'lkml'
        feed_dir.mkdir(parents=True)
        info1 = feed_dir / 'korgalore.lkml.info'
        info1.write_text('{"epochs": {"0": {"last": "abc"}}}')
        info2 = feed_dir / 'korgalore.other-delivery.info'
        info2.write_text('{"epochs": {"0": {"last": "def"}}}')
        # This file should NOT be deleted (not an info file)
        feed_state = feed_dir / 'korgalore.feed'
        feed_state.write_text('{"epochs": {}}')
        # Epoch 0 and a new epoch 1 that appeared while paused
        git_dir = feed_dir / 'git'
        (git_dir / '0.git').mkdir(parents=True)
        (git_dir / '1.git').mkdir(parents=True)

        ctx.invoke(subscribe_resume, feed_key='lkml', skip=True)

        conf_d = conf_d_of(tmp_path)
        assert not (conf_d / 'sub-lkml.toml.paused').exists()
        assert (conf_d / 'sub-lkml.toml').exists()
        # Delivery info files should be deleted
        assert not info1.exists()
        assert not info2.exists()
        # Feed state and epoch directories should be preserved
        assert feed_state.exists()
        assert (git_dir / '0.git').exists()
        assert (git_dir / '1.git').exists()


class TestSubscribeStop:
    """Tests for the subscribe stop command."""

    @pytest.mark.parametrize('start', ['sub-lkml.toml', 'sub-lkml.toml.paused'], ids=['active', 'paused'])
    def test_stop(self, tmp_path: Path, start: str) -> None:
        """subscribe stop removes the config file, active or paused."""
        from korgalore.cli import subscribe_stop

        ctx = sub_ctx(tmp_path, {start: STUB})

        ctx.invoke(subscribe_stop, feed_key='lkml', delete=False)

        assert not (conf_d_of(tmp_path) / start).exists()

    def test_stop_not_found(self, tmp_path: Path) -> None:
        """subscribe stop aborts when subscription not found."""
        from korgalore.cli import subscribe_stop

        ctx = sub_ctx(tmp_path, {})

        with pytest.raises(click.exceptions.Abort):
            ctx.invoke(subscribe_stop, feed_key='nonexistent', delete=False)

    def test_stop_delete(self, tmp_path: Path) -> None:
        """subscribe stop --delete removes config file and feed data."""
        from korgalore.cli import subscribe_stop

        ctx = sub_ctx(tmp_path, {'sub-lkml.toml': STUB})

        # Create feed data directory
        feed_dir = tmp_path / 'data' / 'lkml'
        feed_dir.mkdir(parents=True)
        (feed_dir / 'korgalore.feed').write_text('{}')
        (feed_dir / 'korgalore.lkml.info').write_text('{}')
        (feed_dir / 'git' / '0.git').mkdir(parents=True)

        ctx.invoke(subscribe_stop, feed_key='lkml', delete=True)

        assert not (conf_d_of(tmp_path) / 'sub-lkml.toml').exists()
        assert not feed_dir.exists()
