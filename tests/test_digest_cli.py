"""Tests for the kgl commands around digests: pull, digest, estimate, work.

Everything the commands call is mocked, so these tests check which
deliveries and options reach which function, not what the functions do.
"""

from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import MagicMock, patch

import click
import pytest

from korgalore import AuthenticationError, ConfigurationError
from korgalore.cli import map_deliveries, perform_pull, run_due_digests
from korgalore.digest import DigestSchedule
from korgalore.lei_feed import LeiFeed
from korgalore.pi_feed import PIFeed
from korgalore.summarizer import CommandSummarizer, OpenAISummarizer
from tests.digest_helpers import DAILY, DNAME, digest_cli_env, invoke_digest, make_ctx

DIGESTS = [DNAME, 'netdev-digest']


def _target(identifier: str) -> MagicMock:
    target = MagicMock()
    target.identifier = identifier
    return target


class TestRunDueDigests:
    @staticmethod
    def _obj(targets: Dict[str, MagicMock]) -> Dict[str, Any]:
        return {
            'deliveries': {name: (MagicMock(feed_key=name), t, [], None) for name, t in targets.items()},
            'digest_schedules': {name: DAILY for name in targets},
            'bozofilter': {'bozo@example.com'},
        }

    def test_failure_does_not_stop_others(self) -> None:
        obj = self._obj({'bad': _target('t1'), 'good': _target('t2')})
        good_digest = {'Subject': 'lkml digest', 'Message-ID': '<d@x>'}

        def fake_send(dname: str, *args: Any, **kwargs: Any) -> Any:
            if dname == 'bad':
                raise RuntimeError('boom')
            return [good_digest]

        with patch('korgalore.cli.send_digest', side_effect=fake_send) as mock_send:
            sent = run_due_digests(make_ctx(obj), ['bad', 'good'], force=True)

        assert sent == {'good': ['<d@x>']}
        # Every target used gets disconnected, even after a failure
        for _, target, _, _ in obj['deliveries'].values():
            target.disconnect.assert_called_once()
        assert mock_send.call_args.kwargs == {'force': True}
        assert mock_send.call_args.args[6] == {'bozo@example.com'}

    def test_not_due_is_not_reported(self) -> None:
        obj = self._obj({'quiet': _target('t1')})
        with patch('korgalore.cli.send_digest', return_value=[]):
            assert run_due_digests(make_ctx(obj), ['quiet']) == {}

    def test_auth_error_goes_to_caller(self) -> None:
        obj = self._obj({'gmail': _target('t1')})
        error = AuthenticationError('expired', target_id='t1')
        with patch('korgalore.cli.send_digest', side_effect=error), pytest.raises(AuthenticationError):
            run_due_digests(make_ctx(obj), ['gmail'])


LOCAL = {'type': 'openai', 'url': 'http://localhost:11434/v1', 'model': 'qwen3:32b'}
REMOTE = {'type': 'openai', 'url': 'https://llm.example.org/v1', 'model': 'big'}
COMMAND = {'type': 'command', 'command': 'llm -m local'}


def lei_feed() -> MagicMock:
    return MagicMock(spec=LeiFeed, feed_key='lei')


class TestMapDeliveries:
    @staticmethod
    def _map(details: Dict[str, Any]) -> click.Context:
        ctx = make_ctx({'config': {'targets': {}}, 'targets': {}, 'feeds': {}})
        with (
            patch('korgalore.cli.get_feed_for_delivery', return_value=MagicMock(feed_key='lkml')),
            patch('korgalore.cli.get_target', return_value=_target('local')),
        ):
            map_deliveries(ctx, {DNAME: {'feed': 'lkml', 'target': 'local', **details}})
        return ctx

    @staticmethod
    def _map_summarized(
        summarizers: Dict[str, Any], feed: Any = None, names: Tuple[str, ...] = (DNAME,)
    ) -> click.Context:
        ctx = make_ctx({'config': {'targets': {}, 'summarizers': summarizers}, 'targets': {}, 'feeds': {}})
        if feed is None:
            feed = MagicMock(feed_key='lkml')
        details = {'feed': 'lkml', 'target': 'local', 'mode': 'digest', 'summarizer': 'local'}
        with (
            patch('korgalore.cli.get_feed_for_delivery', return_value=feed),
            patch('korgalore.cli.get_target', return_value=_target('local')),
        ):
            map_deliveries(ctx, {name: dict(details) for name in names})
        return ctx

    def test_message_is_default(self) -> None:
        assert self._map({}).obj['digest_schedules'] == {}

    def test_digest(self) -> None:
        ctx = self._map({'mode': 'digest', 'schedule': 'weekly'})
        assert ctx.obj['digest_schedules'] == {DNAME: DigestSchedule(schedule='weekly')}
        assert DNAME in ctx.obj['deliveries']

    @pytest.mark.parametrize(
        ('details', 'match'),
        [
            pytest.param({'mode': 'digests'}, 'mode', id='bad-mode'),
            # A typo in mode shouldn't silently deliver every message
            pytest.param({'send_at': '07:00'}, 'send_at', id='digest-keys-need-digest-mode'),
        ],
    )
    def test_bad_delivery_config(self, details: Dict[str, Any], match: str) -> None:
        with pytest.raises(ConfigurationError, match=match):
            self._map(details)

    def test_summarizer(self) -> None:
        ctx = self._map_summarized({'local': LOCAL}, names=(DNAME, 'netdev-digest'))
        summarizer = ctx.obj['summarizers']['local']
        assert isinstance(summarizer, OpenAISummarizer)
        assert summarizer.model == 'qwen3:32b'
        assert list(ctx.obj['summarizers']) == ['local']
        assert ctx.obj['digest_schedules'][DNAME].summarizer == 'local'

    def test_plain_digests_need_no_summarizers(self) -> None:
        assert self._map({'mode': 'digest'}).obj['summarizers'] == {}

    def test_lore_feed_may_use_a_remote_summarizer(self) -> None:
        ctx = self._map_summarized({'local': REMOTE})
        assert not ctx.obj['summarizers']['local'].is_local

    @pytest.mark.parametrize(
        ('summarizers', 'lei', 'match'),
        [
            pytest.param({'other': LOCAL}, False, "summarizer 'local' is not defined", id='unknown'),
            pytest.param({'local': {'type': 'openai', 'model': 'm'}}, False, "Summarizer 'local': url", id='bad'),
            # lei can find private mail
            pytest.param({'local': REMOTE}, True, 'allow_private_feeds', id='lei-remote'),
            pytest.param({'local': COMMAND}, True, 'allow_private_feeds', id='lei-command'),
        ],
    )
    def test_summarizer_refused(self, summarizers: Dict[str, Any], lei: bool, match: str) -> None:
        with pytest.raises(ConfigurationError, match=match):
            self._map_summarized(summarizers, feed=lei_feed() if lei else None)

    @pytest.mark.parametrize(
        ('details', 'kind', 'local'),
        [
            pytest.param(LOCAL, OpenAISummarizer, True, id='local'),
            pytest.param({**COMMAND, 'allow_private_feeds': True}, CommandSummarizer, False, id='allow-private-feeds'),
        ],
    )
    def test_lei_feed_summarizer_accepted(self, details: Dict[str, Any], kind: type, local: bool) -> None:
        summarizer = self._map_summarized({'local': details}, feed=lei_feed()).obj['summarizers']['local']
        assert isinstance(summarizer, kind)
        if local:
            assert isinstance(summarizer, OpenAISummarizer)
            assert summarizer.is_local


def _pull_obj() -> Dict[str, Any]:
    feed = MagicMock(feed_key='lkml')
    feed.update_feed.return_value = PIFeed.STATUS_UPDATED
    feed.STATUS_UPDATED = PIFeed.STATUS_UPDATED
    feed.STATUS_INITIALIZED = PIFeed.STATUS_INITIALIZED
    feed.STATUS_NOCHANGE = PIFeed.STATUS_NOCHANGE
    feed.get_latest_commits_for_delivery.return_value = [(0, 'abc')]
    target = _target('local')
    return {
        'config': {'deliveries': {'lkml-all': {}, DNAME: {'mode': 'digest'}}},
        'feeds': {'lkml': feed},
        'deliveries': {'lkml-all': (feed, target, [], None), DNAME: (feed, target, [], None)},
        'digest_schedules': {DNAME: DAILY},
        'targets': {},
        'bozofilter': set(),
        'hide_bar': True,
    }


@pytest.fixture
def pull_env() -> Iterator[Tuple[MagicMock, MagicMock]]:
    """Stub out mapping, locking and tracking; yield (deliver_commit, run_due_digests)."""
    deliver = MagicMock(return_value='<m@x>')
    digests = MagicMock(return_value={DNAME: ['<digest-1@x>', '<digest-2@x>']})
    with (
        patch('korgalore.cli.map_deliveries'),
        patch('korgalore.cli.map_tracked_threads'),
        patch('korgalore.cli.lock_all_feeds'),
        patch('korgalore.cli.unlock_all_feeds') as unlock,
        patch('korgalore.cli.retry_all_failed_deliveries'),
        patch('korgalore.cli.update_tracked_thread_activity'),
        patch('korgalore.cli.close_requests_session'),
        patch('korgalore.cli.deliver_commit', deliver),
        patch('korgalore.cli.run_due_digests', digests),
    ):
        digests.unlock = unlock
        yield deliver, digests


class TestPullHook:
    @pytest.mark.parametrize(
        ('no_update', 'force'),
        [pytest.param(False, False, id='plain'), pytest.param(True, True, id='force-no-update')],
    )
    def test_digest_not_delivered_per_message(
        self, pull_env: Tuple[MagicMock, MagicMock], no_update: bool, force: bool
    ) -> None:
        deliver, digests = pull_env
        changes, msgids = perform_pull(make_ctx(_pull_obj()), no_update=no_update, force=force, delivery_name=None)

        assert [c.args[0] for c in deliver.call_args_list] == ['lkml-all']
        assert digests.call_args.args[1] == [DNAME]
        # A digest in two parts counts as two delivered messages
        assert changes == {'lkml-all': 1, DNAME: 2}
        assert msgids == {'<m@x>', '<digest-1@x>', '<digest-2@x>'}

    def test_digests_checked_without_updates(self, pull_env: Tuple[MagicMock, MagicMock]) -> None:
        # A digest can be due on a quiet day, when no feed has news
        deliver, digests = pull_env
        obj = _pull_obj()
        obj['feeds']['lkml'].update_feed.return_value = PIFeed.STATUS_NOCHANGE

        changes, _ = perform_pull(make_ctx(obj), no_update=False, force=False, delivery_name=None)

        deliver.assert_not_called()
        digests.assert_called_once()
        assert changes == {DNAME: 2}

    def test_auth_error_unlocks(self, pull_env: Tuple[MagicMock, MagicMock]) -> None:
        _, digests = pull_env
        digests.side_effect = AuthenticationError('expired', target_id='local')
        with pytest.raises(AuthenticationError):
            perform_pull(make_ctx(_pull_obj()), no_update=True, force=False, delivery_name=None)
        digests.unlock.assert_called_once()


def cli_obj() -> Dict[str, Any]:
    """The ctx.obj of kgl: one message delivery and two digests."""
    return {
        'config': {
            'deliveries': {
                'lkml-all': {'feed': 'lkml', 'target': 'local'},
                DNAME: {'feed': 'lkml', 'target': 'local', 'mode': 'digest'},
                'netdev-digest': {'feed': 'netdev', 'target': 'local', 'mode': 'digest'},
            }
        },
        'targets': {},
        'hide_bar': True,
    }


class TestDigestCommand:
    @pytest.fixture
    def env(self) -> Iterator[Dict[str, MagicMock]]:
        def fake_update(ctx: click.Context, **kwargs: Any) -> Tuple[List[str], List[str]]:
            ctx.obj['failed_feeds'] = ['netdev']
            return [], []

        with digest_cli_env('map', 'lock', 'unlock', 'update', 'due', 'close') as mocks:
            mocks['update'].side_effect = fake_update
            mocks['due'].return_value = {}
            yield mocks

    def test_runs_only_digests(self, env: Dict[str, MagicMock]) -> None:
        result = invoke_digest(cli_obj())
        assert result.exit_code == 0, result.output
        assert list(env['map'].call_args.args[1]) == DIGESTS
        assert env['due'].call_args.args[1] == DIGESTS
        assert env['due'].call_args.kwargs == {'force': False}
        env['update'].assert_called_once()
        env['unlock'].assert_called_once()

    def test_named_and_forced(self, env: Dict[str, MagicMock]) -> None:
        result = invoke_digest(cli_obj(), '--force', '--no-update', DNAME)
        assert result.exit_code == 0, result.output
        assert env['due'].call_args.args[1] == [DNAME]
        assert env['due'].call_args.kwargs == {'force': True}
        env['update'].assert_not_called()

    @pytest.mark.parametrize('name', ['nope', 'lkml-all'], ids=['unknown', 'message-delivery'])
    def test_delivery_refused(self, env: Dict[str, MagicMock], name: str) -> None:
        result = invoke_digest(cli_obj(), name)
        assert result.exit_code != 0
        env['due'].assert_not_called()

    def test_fail_on_feed_error(self, env: Dict[str, MagicMock]) -> None:
        assert invoke_digest(cli_obj()).exit_code == 0
        result = invoke_digest(cli_obj(), '--fail-on-feed-error')
        assert result.exit_code == 3
        # The digests were still sent
        assert env['due'].call_count == 2

    def test_unlocks_on_error(self, env: Dict[str, MagicMock]) -> None:
        env['due'].side_effect = AuthenticationError('expired', target_id='local')
        result = invoke_digest(cli_obj())
        assert result.exit_code != 0
        env['unlock'].assert_called_once()

    def test_no_digests_configured(self, env: Dict[str, MagicMock]) -> None:
        obj = cli_obj()
        del obj['config']['deliveries'][DNAME]
        del obj['config']['deliveries']['netdev-digest']
        result = invoke_digest(obj)
        assert result.exit_code == 0
        env['map'].assert_not_called()


class TestEstimateCommand:
    @pytest.fixture
    def env(self) -> Iterator[Dict[str, MagicMock]]:
        with digest_cli_env('map', 'lock', 'unlock', 'update', 'estimate', 'due', 'work') as mocks:
            yield mocks

    def test_estimate_sends_nothing(self, env: Dict[str, MagicMock]) -> None:
        result = invoke_digest(cli_obj(), '--estimate')
        assert result.exit_code == 0, result.output
        assert env['estimate'].call_args.args[1] == DIGESTS
        env['due'].assert_not_called()
        # Reading the feed needs the lock, like a real digest
        env['lock'].assert_called_once()
        env['unlock'].assert_called_once()
        env['update'].assert_called_once()

    def test_no_update(self, env: Dict[str, MagicMock]) -> None:
        assert invoke_digest(cli_obj(), '--estimate', '--no-update').exit_code == 0
        env['update'].assert_not_called()
        env['estimate'].assert_called_once()

    @pytest.mark.parametrize('other', ['--force', '--work'])
    def test_estimate_only_reports(self, env: Dict[str, MagicMock], other: str) -> None:
        result = invoke_digest(cli_obj(), '--estimate', other)
        assert result.exit_code == 2
        assert 'only reports' in result.output
        env['map'].assert_not_called()
        env['estimate'].assert_not_called()
        env['due'].assert_not_called()
        env['work'].assert_not_called()


class TestWorkCommand:
    @pytest.fixture
    def env(self) -> Iterator[Dict[str, MagicMock]]:
        with digest_cli_env('map', 'lock', 'update', 'work') as mocks:
            yield mocks

    def test_work_takes_no_feed_locks(self, env: Dict[str, MagicMock]) -> None:
        result = invoke_digest(cli_obj(), '--work')
        assert result.exit_code == 0, result.output
        assert env['work'].call_args.args[1] == DIGESTS
        env['lock'].assert_not_called()
        env['update'].assert_not_called()

    def test_work_with_force_is_refused(self, env: Dict[str, MagicMock]) -> None:
        result = invoke_digest(cli_obj(), '--work', '--force')
        assert result.exit_code == 2
        env['work'].assert_not_called()
