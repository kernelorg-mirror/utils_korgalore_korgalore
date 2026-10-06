"""Tests for digest schedules: reading the config and deciding when to send.

Send times are wall-clock times in the local time zone. The tests set
TZ=America/Montreal so the results don't depend on the machine running
them, and so we can check what happens when daylight saving time starts
and ends.
"""

import time as time_mod
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterator

import pytest

from korgalore import ConfigurationError
from korgalore.digest import DEFAULT_FROM, DigestSchedule

# Montreal is UTC-4 in summer (EDT) and UTC-5 in winter (EST)
EDT = timezone(timedelta(hours=-4))
EST = timezone(timedelta(hours=-5))


@pytest.fixture(autouse=True)
def montreal_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test in the America/Montreal time zone."""
    monkeypatch.setenv('TZ', 'America/Montreal')
    time_mod.tzset()
    yield
    monkeypatch.undo()
    time_mod.tzset()


def parse(**details: Any) -> DigestSchedule:
    return DigestSchedule.from_config('lkml-digest', details)


class TestFromConfig:
    def test_defaults(self) -> None:
        sched = parse()
        assert sched == DigestSchedule()
        assert sched.schedule == 'daily'
        assert sched.send_at == time(7, 0)
        assert sched.send_day == 0
        assert sched.send_empty is False
        assert sched.from_addr == DEFAULT_FROM
        assert sched.period == timedelta(days=1)
        assert sched.summarizer is None
        assert sched.max_summaries is None
        assert not sched.needs_worker

    @pytest.mark.parametrize(
        ('details', 'expected', 'period'),
        [
            pytest.param(
                {
                    'schedule': 'weekly',
                    'send_at': '18:30',
                    'send_day': 'Friday',
                    'send_empty': True,
                    'digest_from': 'Digests <me@example.org>',
                },
                DigestSchedule('weekly', time(18, 30), 4, True, 'Digests <me@example.org>'),
                timedelta(weeks=1),
                id='all-keys',
            ),
            pytest.param(
                {'send_at': '7:05'}, DigestSchedule(send_at=time(7, 5)), timedelta(days=1), id='single-digit-hour'
            ),
        ],
    )
    def test_all_keys(self, details: Dict[str, Any], expected: DigestSchedule, period: timedelta) -> None:
        sched = parse(**details)
        assert sched == expected
        assert sched.period == period

    def test_summarizer(self) -> None:
        sched = parse(summarizer='local', max_summaries=25)
        assert sched.summarizer == 'local'
        assert sched.max_summaries == 25
        assert sched.needs_worker

    def test_summary_instructions(self) -> None:
        sched = parse(summarizer='local', summary_instructions='  Tell me if anyone sounds upset.\n')
        assert sched.summary_instructions == 'Tell me if anyone sounds upset.'
        assert parse(summarizer='local').summary_instructions is None

    @pytest.mark.parametrize('day,expected', [('mon', 0), ('SUN', 6), ('wednesday', 2), ('Thurs', 3)])
    def test_send_day_spellings(self, day: str, expected: int) -> None:
        assert parse(schedule='weekly', send_day=day).send_day == expected

    @pytest.mark.parametrize(
        'details,key',
        [
            ({'schedule': 'hourly'}, 'schedule'),
            ({'send_at': '24:00'}, 'send_at'),
            ({'send_at': '7am'}, 'send_at'),
            ({'send_at': '07:00:00'}, 'send_at'),
            ({'send_at': 700}, 'send_at'),
            ({'send_day': 'mon'}, 'send_day'),
            ({'schedule': 'weekly', 'send_day': 'someday'}, 'send_day'),
            ({'schedule': 'weekly', 'send_day': 'monkey'}, 'send_day'),
            ({'schedule': 'weekly', 'send_day': 'mo'}, 'send_day'),
            ({'schedule': 'weekly', 'send_day': 1}, 'send_day'),
            ({'send_empty': 'yes'}, 'send_empty'),
            ({'digest_from': 'korgalore'}, 'digest_from'),
            ({'digest_from': ['me@example.org']}, 'digest_from'),
            ({'summarizer': ''}, 'summarizer'),
            ({'summarizer': ['local']}, 'summarizer'),
            ({'max_summaries': 10}, 'max_summaries'),
            ({'summarizer': 'local', 'max_summaries': 0}, 'max_summaries'),
            ({'summarizer': 'local', 'max_summaries': '10'}, 'max_summaries'),
            ({'summarizer': 'local', 'max_summaries': True}, 'max_summaries'),
            ({'summary_instructions': 'Tell me if anyone sounds upset.'}, 'summary_instructions'),
            ({'summarizer': 'local', 'summary_instructions': ''}, 'summary_instructions'),
            ({'summarizer': 'local', 'summary_instructions': ' \n'}, 'summary_instructions'),
            (
                {'summarizer': 'local', 'summary_instructions': ['Tell me if anyone sounds upset.']},
                'summary_instructions',
            ),
        ],
    )
    def test_bad_values(self, details: Dict[str, Any], key: str) -> None:
        with pytest.raises(ConfigurationError) as exc:
            parse(**details)
        # The message names the delivery and the key, so it's easy to fix
        assert "'lkml-digest'" in str(exc.value)
        assert key in str(exc.value)


class TestDaily:
    sched = DigestSchedule(send_at=time(7, 0))

    @pytest.mark.parametrize(
        ('now', 'expected'),
        [
            pytest.param(
                datetime(2026, 10, 1, 9, 0, tzinfo=EDT),
                datetime(2026, 10, 1, 7, 0, tzinfo=EDT),
                id='today-after-send-time',
            ),
            pytest.param(
                datetime(2026, 10, 1, 6, 59, tzinfo=EDT),
                datetime(2026, 9, 30, 7, 0, tzinfo=EDT),
                id='yesterday-before-send-time',
            ),
            pytest.param(
                datetime(2026, 10, 1, 7, 0, tzinfo=EDT), datetime(2026, 10, 1, 7, 0, tzinfo=EDT), id='exact-send-time'
            ),
            # 11:30 UTC is 07:30 in Montreal, so today's slot has passed
            pytest.param(
                datetime(2026, 10, 1, 11, 30, tzinfo=timezone.utc),
                datetime(2026, 10, 1, 7, 0, tzinfo=EDT),
                id='utc-input-uses-local-wall-clock',
            ),
        ],
    )
    def test_last_slot(self, now: datetime, expected: datetime) -> None:
        assert self.sched.last_slot(now) == expected

    def test_first_run_is_due(self) -> None:
        assert self.sched.is_due(None, datetime(2026, 10, 1, 3, 0, tzinfo=EDT))

    def test_not_due_after_sending(self) -> None:
        sent = datetime(2026, 10, 1, 7, 0, 5, tzinfo=EDT)
        assert not self.sched.is_due(sent, sent + timedelta(minutes=10))
        assert not self.sched.is_due(sent, datetime(2026, 10, 2, 6, 59, tzinfo=EDT))
        assert self.sched.is_due(sent, datetime(2026, 10, 2, 7, 0, tzinfo=EDT))

    def test_missed_days_send_one_digest(self) -> None:
        # The laptop was asleep for three days: one digest, not three
        sent = datetime(2026, 9, 27, 7, 0, 5, tzinfo=EDT)
        now = datetime(2026, 10, 1, 10, 0, tzinfo=EDT)
        assert self.sched.is_due(sent, now)
        assert not self.sched.is_due(now, now + timedelta(minutes=5))

    def test_period_start(self) -> None:
        now = datetime(2026, 10, 1, 7, 0, tzinfo=EDT)
        sent = datetime(2026, 9, 29, 7, 0, tzinfo=EDT)
        assert self.sched.period_start(None, now) == now - timedelta(days=1)
        assert self.sched.period_start(sent, now) == sent


class TestWeekly:
    # Fridays at 18:00; 2026-10-02 is a Friday
    sched = DigestSchedule(schedule='weekly', send_at=time(18, 0), send_day=4)

    @pytest.mark.parametrize(
        ('now', 'expected'),
        [
            pytest.param(
                datetime(2026, 10, 2, 19, 0, tzinfo=EDT),
                datetime(2026, 10, 2, 18, 0, tzinfo=EDT),
                id='send-day-after-time',
            ),
            pytest.param(
                datetime(2026, 10, 2, 17, 0, tzinfo=EDT),
                datetime(2026, 9, 25, 18, 0, tzinfo=EDT),
                id='send-day-before-time',
            ),
            pytest.param(
                datetime(2026, 10, 6, 12, 0, tzinfo=EDT), datetime(2026, 10, 2, 18, 0, tzinfo=EDT), id='midweek'
            ),
        ],
    )
    def test_last_slot(self, now: datetime, expected: datetime) -> None:
        assert self.sched.last_slot(now) == expected

    def test_due_once_a_week(self) -> None:
        sent = datetime(2026, 10, 2, 18, 0, 5, tzinfo=EDT)
        assert not self.sched.is_due(sent, datetime(2026, 10, 9, 17, 59, tzinfo=EDT))
        assert self.sched.is_due(sent, datetime(2026, 10, 9, 18, 0, tzinfo=EDT))

    def test_first_run_backfills_a_week(self) -> None:
        now = datetime(2026, 10, 2, 18, 0, tzinfo=EDT)
        assert self.sched.period_start(None, now) == now - timedelta(weeks=1)


class TestDaylightSaving:
    """07:00 stays 07:00 on the local clock when the UTC offset changes."""

    sched = DigestSchedule(send_at=time(7, 0))

    @pytest.mark.parametrize(
        ('now', 'expected'),
        [
            # DST starts on 2026-03-08 at 02:00
            pytest.param(
                datetime(2026, 3, 8, 8, 0, tzinfo=EDT), datetime(2026, 3, 8, 7, 0, tzinfo=EDT), id='spring-forward'
            ),
            # The day before was still in winter time
            pytest.param(
                datetime(2026, 3, 8, 6, 0, tzinfo=EDT),
                datetime(2026, 3, 7, 7, 0, tzinfo=EST),
                id='spring-forward-before',
            ),
            # DST ends on 2026-11-01 at 02:00
            pytest.param(
                datetime(2026, 11, 1, 8, 0, tzinfo=EST), datetime(2026, 11, 1, 7, 0, tzinfo=EST), id='fall-back'
            ),
            pytest.param(
                datetime(2026, 11, 1, 6, 0, tzinfo=EST), datetime(2026, 10, 31, 7, 0, tzinfo=EDT), id='fall-back-before'
            ),
        ],
    )
    def test_last_slot(self, now: datetime, expected: datetime) -> None:
        assert self.sched.last_slot(now) == expected

    def test_fall_back_is_due_once(self) -> None:
        # The day is 25 hours long, but there is still one digest
        sent = datetime(2026, 10, 31, 7, 0, 5, tzinfo=EDT)
        assert not self.sched.is_due(sent, datetime(2026, 11, 1, 6, 59, tzinfo=EST))
        assert self.sched.is_due(sent, datetime(2026, 11, 1, 7, 0, tzinfo=EST))

    def test_weekly_across_dst(self) -> None:
        # Sundays at 07:00: one slot before and one after the change
        sched = DigestSchedule(schedule='weekly', send_day=6)
        now = datetime(2026, 11, 3, 12, 0, tzinfo=EST)
        assert sched.last_slot(now) == datetime(2026, 11, 1, 7, 0, tzinfo=EST)
        assert sched.last_slot(now - timedelta(days=3)) == datetime(2026, 10, 25, 7, 0, tzinfo=EDT)
