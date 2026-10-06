"""Tests for MAINTAINERS file parser and query builders."""

import logging
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Type

import pytest

from korgalore.maintainers import (
    SubsystemEntry,
    Tree,
    build_mailinglist_query,
    build_maintainers_query,
    build_patches_query,
    email_to_list_id,
    extract_email,
    generate_subsystem_config,
    get_subsystem,
    is_field_line,
    is_simple_pattern,
    is_subsystem_title,
    normalize_subsystem_name,
    parse_maintainers,
    parse_tree,
)


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        pytest.param('AMD GPU', 'amd_gpu', id='basic'),
        pytest.param('9P FILE SYSTEM', '9p_file_system', id='leading-digit'),
        pytest.param(
            '3WARE SAS/SATA-RAID SCSI DRIVERS (3W-XXXX)', '3ware_sas_sata_raid_scsi_drivers', id='drops-parenthetical'
        ),
        pytest.param('SAS/SATA-RAID', 'sas_sata_raid', id='special-chars'),
        pytest.param('  SUBSYSTEM  ', 'subsystem', id='strips-edges'),
        pytest.param('MULTIPLE   SPACES', 'multiple_spaces', id='collapses-spaces'),
    ],
)
def test_normalize_subsystem_name(name: str, expected: str) -> None:
    assert normalize_subsystem_name(name) == expected


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        pytest.param('John Doe <john@example.com>', 'john@example.com', id='angle-brackets'),
        pytest.param('"John Doe" <john@example.com>', 'john@example.com', id='quoted-name'),
        # MAINTAINERS puts a comment after some list addresses
        pytest.param('list@domain.org (moderated for non-subscribers)', 'list@domain.org', id='list-with-comment'),
        pytest.param('john@example.com', 'john@example.com', id='bare'),
        pytest.param('', None, id='empty'),
        pytest.param('   ', None, id='whitespace-only'),
        pytest.param('Name <>', None, id='empty-angle-brackets'),
        pytest.param('Name <user+tag@sub.domain.org>', 'user+tag@sub.domain.org', id='complex'),
    ],
)
def test_extract_email(value: str, expected: Optional[str]) -> None:
    assert extract_email(value) == expected


@pytest.mark.parametrize(
    ('line', 'expected'),
    [
        pytest.param('M:\tJohn Doe <john@example.com>', True, id='maintainer'),
        pytest.param('R:\tReviewer <reviewer@example.com>', True, id='reviewer'),
        pytest.param('L:\tlist@lists.linux.dev', True, id='list'),
        pytest.param('F:\tdrivers/gpu/', True, id='file'),
        pytest.param('S:\tMaintained', True, id='status'),
        pytest.param('X:\tdrivers/staging/', True, id='excluded'),
        pytest.param('T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/test.git', True, id='tree'),
        pytest.param('m:\tvalue', False, id='lowercase-prefix'),
        pytest.param('M: value', False, id='space-not-tab'),
        pytest.param('M:value', False, id='no-separator'),
        pytest.param('MM:\tvalue', False, id='multi-char-prefix'),
        pytest.param('', False, id='empty'),
        pytest.param('M:', False, id='too-short'),
        pytest.param('M', False, id='single-char'),
    ],
)
def test_is_field_line(line: str, expected: bool) -> None:
    assert is_field_line(line) is expected


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        pytest.param(
            'git git://git.kernel.org/pub/scm/linux/kernel/git/test.git',
            Tree('git', 'git://git.kernel.org/pub/scm/linux/kernel/git/test.git'),
            id='plain-git',
        ),
        pytest.param(
            'git git://git.kernel.org/pub/scm/linux/kernel/git/test.git for-next',
            Tree('git', 'git://git.kernel.org/pub/scm/linux/kernel/git/test.git', branch='for-next'),
            id='with-branch',
        ),
        pytest.param(
            'quilt https://example.com/quilt-tree/', Tree('quilt', 'https://example.com/quilt-tree/'), id='non-git-vcs'
        ),
        # Real entries do this, e.g. 'T:\\tgit git://.../pm.git (For ARM Updates)'
        pytest.param(
            'git git://git.kernel.org/pub/scm/linux/kernel/git/pm.git (For ARM Updates)',
            Tree('git', 'git://git.kernel.org/pub/scm/linux/kernel/git/pm.git'),
            id='parenthetical-note-is-not-a-branch',
        ),
        # A handful of real entries omit the vcs keyword; the URL must not be read as the vcs
        pytest.param(
            'https://git.kernel.org/pub/scm/linux/kernel/git/leitao/linux.git configfs-next',
            Tree('', 'https://git.kernel.org/pub/scm/linux/kernel/git/leitao/linux.git', branch='configfs-next'),
            id='missing-vcs-keyword',
        ),
        pytest.param(
            'git://git.kernel.org/pub/scm/linux/kernel/git/sched_ext.git',
            Tree('', 'git://git.kernel.org/pub/scm/linux/kernel/git/sched_ext.git'),
            id='url-only',
        ),
        pytest.param('', Tree('', ''), id='empty-value'),
    ],
)
def test_parse_tree(value: str, expected: Tree) -> None:
    assert parse_tree(value) == expected


@pytest.mark.parametrize(
    ('line', 'prev_empty', 'expected'),
    [
        pytest.param('AMD GPU DRIVER', True, True, id='uppercase'),
        pytest.param('9P FILE SYSTEM', True, True, id='leading-digit'),
        pytest.param('AMD GPU DRIVER', False, False, id='no-empty-line-before'),
        pytest.param('AMD\tGPU', True, False, id='tab-inside'),
        pytest.param('M:\tvalue', True, False, id='field-line'),
        pytest.param('M:\tJohn Doe', True, False, id='field-line-with-name'),
        pytest.param('all lowercase words', True, False, id='no-uppercase'),
        pytest.param('', True, False, id='empty'),
        pytest.param('   ', True, False, id='whitespace-only'),
        # Many subsystems use mixed case
        pytest.param('ARM/Allwinner SoC Clock Support', True, True, id='mixed-case-slash'),
        pytest.param('AMD Gpu Driver', True, True, id='mixed-case'),
        pytest.param('3WARE SAS/SATA-RAID SCSI DRIVERS (3W-XXXX)', True, True, id='slashes-and-parens'),
        pytest.param('ACPI FOR ARM64 (ACPI/arm64)', True, True, id='parens'),
    ],
)
def test_is_subsystem_title(line: str, prev_empty: bool, expected: bool) -> None:
    assert is_subsystem_title(line, prev_line_empty=prev_empty) is expected


@pytest.mark.parametrize(
    ('pattern', 'expected'),
    [
        pytest.param('csky', True, id='word'),
        pytest.param('driver', True, id='another-word'),
        pytest.param('drivers/gpu/drm', True, id='path'),
        pytest.param(r'\bword\b', False, id='backslash'),
        pytest.param('[0-9]', False, id='brackets'),
        pytest.param('(group)', False, id='parens'),
        pytest.param('a*', False, id='star'),
        pytest.param('a+', False, id='plus'),
        pytest.param('a?', False, id='question-mark'),
        pytest.param('^start', False, id='start-anchor'),
        pytest.param('end$', False, id='end-anchor'),
        pytest.param('a|b', False, id='alternation'),
        pytest.param(r'\b(?i:clang|llvm)\b', False, id='complex-clang'),
        pytest.param(r'[^\s]+\.rs$', False, id='complex-rs'),
    ],
)
def test_is_simple_pattern(pattern: str, expected: bool) -> None:
    assert is_simple_pattern(pattern) is expected


@pytest.mark.parametrize(
    ('email', 'expected'),
    [
        pytest.param('v9fs@lists.linux.dev', 'v9fs.lists.linux.dev', id='basic'),
        pytest.param('linux-kernel@vger.kernel.org', 'linux-kernel.vger.kernel.org', id='subdomain'),
    ],
)
def test_email_to_list_id(email: str, expected: str) -> None:
    assert email_to_list_id(email) == expected


@pytest.mark.parametrize(
    ('kwargs', 'since', 'expected'),
    [
        pytest.param(
            {'maintainers': ['john@example.com']}, '30.days.ago', 'a:john@example.com AND d:30.days.ago..', id='single'
        ),
        # Also shows that a custom since is used
        pytest.param(
            {'maintainers': ['john@example.com', 'jane@example.com']},
            '7.days.ago',
            '(a:john@example.com OR a:jane@example.com) AND d:7.days.ago..',
            id='multiple-or-parens',
        ),
        pytest.param(
            {'maintainers': ['maintainer@example.com'], 'reviewers': ['reviewer@example.com']},
            '30.days.ago',
            '(a:maintainer@example.com OR a:reviewer@example.com) AND d:30.days.ago..',
            id='maintainers-and-reviewers',
        ),
        pytest.param({}, '30.days.ago', None, id='nobody'),
    ],
)
def test_build_maintainers_query(kwargs: Dict[str, Any], since: str, expected: Optional[str]) -> None:
    assert build_maintainers_query(SubsystemEntry(name='TEST', **kwargs), since) == expected


class TestBuildMailinglistQuery:
    """Tests for build_mailinglist_query function."""

    def test_single_list(self) -> None:
        """Query with single mailing list."""
        entry = SubsystemEntry(
            name='TEST',
            mailing_lists=['list@lists.linux.dev'],
        )
        query, excluded = build_mailinglist_query(entry, '30.days.ago')
        assert query == 'l:list.lists.linux.dev AND d:30.days.ago..'
        assert excluded == []

    def test_multiple_lists(self) -> None:
        """Query with multiple mailing lists uses OR and parentheses."""
        entry = SubsystemEntry(
            name='TEST',
            mailing_lists=['list1@domain.org', 'list2@domain.org'],
        )
        query, excluded = build_mailinglist_query(entry, '30.days.ago')
        assert query == '(l:list1.domain.org OR l:list2.domain.org) AND d:30.days.ago..'
        assert excluded == []

    def test_no_lists(self) -> None:
        """Returns None when no mailing lists."""
        entry = SubsystemEntry(name='TEST')
        query, excluded = build_mailinglist_query(entry, '30.days.ago')
        assert query is None
        assert excluded == []

    def test_excludes_default_catchall_lists(self) -> None:
        """Default catchall lists are excluded from query."""
        entry = SubsystemEntry(
            name='TEST',
            mailing_lists=[
                'subsystem@lists.linux.dev',
                'linux-kernel@vger.kernel.org',
                'patches@lists.linux.dev',
            ],
        )
        query, excluded = build_mailinglist_query(entry, '30.days.ago')
        assert query == 'l:subsystem.lists.linux.dev AND d:30.days.ago..'
        assert 'linux-kernel@vger.kernel.org' in excluded
        assert 'patches@lists.linux.dev' in excluded

    def test_all_lists_excluded_returns_none(self) -> None:
        """Returns None when all lists are catchall lists."""
        entry = SubsystemEntry(
            name='TEST',
            mailing_lists=['linux-kernel@vger.kernel.org'],
        )
        query, excluded = build_mailinglist_query(entry, '30.days.ago')
        assert query is None
        assert excluded == ['linux-kernel@vger.kernel.org']

    @pytest.mark.parametrize('catchall', [{'custom@example.com'}, set()], ids=['custom', 'empty'])
    def test_catchall_override_is_used(self, catchall: Set[str]) -> None:
        """A custom or empty catchall set replaces the default, so nothing is excluded."""
        entry = SubsystemEntry(name='TEST', mailing_lists=['subsystem@lists.linux.dev', 'linux-kernel@vger.kernel.org'])
        query, excluded = build_mailinglist_query(entry, '30.days.ago', catchall_lists=catchall)
        assert query == '(l:subsystem.lists.linux.dev OR l:linux-kernel.vger.kernel.org) AND d:30.days.ago..'
        assert excluded == []


@pytest.mark.parametrize(
    ('kwargs', 'query', 'skipped'),
    [
        pytest.param(
            {'files': ['drivers/gpu/', 'include/drm/']},
            '(dfn:drivers/gpu/ OR dfn:include/drm/) AND d:30.days.ago..',
            [],
            id='files-keep-trailing-slash',
        ),
        pytest.param(
            {'files': ['drivers/'], 'excluded': ['drivers/staging/']},
            'dfn:drivers/ NOT dfn:drivers/staging/ AND d:30.days.ago..',
            [],
            id='excluded',
        ),
        pytest.param({'file_regex': ['csky']}, 'dfn:csky AND d:30.days.ago..', [], id='simple-file-regex'),
        pytest.param({'file_regex': [r'\b(?i:clang)\b']}, None, [r'N:\b(?i:clang)\b'], id='complex-file-regex-skipped'),
        pytest.param(
            {'content_regex': ['CONFIG_DRM']}, 'dfb:CONFIG_DRM AND d:30.days.ago..', [], id='simple-content-regex'
        ),
        pytest.param({'content_regex': [r'[^\s]+\.rs$']}, None, [r'K:[^\s]+\.rs$'], id='complex-content-regex-skipped'),
        pytest.param(
            {'files': ['drivers/test/'], 'file_regex': ['simple', r'complex\b'], 'content_regex': ['CONFIG_TEST']},
            '(dfn:drivers/test/ OR dfn:simple OR dfb:CONFIG_TEST) AND d:30.days.ago..',
            [r'N:complex\b'],
            id='mixed-simple-and-complex',
        ),
        pytest.param({}, None, [], id='no-patterns'),
        pytest.param({'files': ['drivers/test/']}, 'dfn:drivers/test/ AND d:30.days.ago..', [], id='single-no-parens'),
        pytest.param(
            {'files': ['drivers/a/', 'drivers/b/']},
            '(dfn:drivers/a/ OR dfn:drivers/b/) AND d:30.days.ago..',
            [],
            id='multiple-parens-with-or',
        ),
    ],
)
def test_build_patches_query(kwargs: Dict[str, Any], query: Optional[str], skipped: List[str]) -> None:
    assert build_patches_query(SubsystemEntry(name='TEST', **kwargs), '30.days.ago') == (query, skipped)


class TestParseMaintainers:
    """Tests for parse_maintainers function."""

    def test_parse_list_with_comment(self, tmp_path: Path) -> None:
        """A comment after an L: address is dropped, not searched for."""
        maintainers = tmp_path / 'MAINTAINERS'
        maintainers.write_text("""
ARM SUB-ARCHITECTURES
M:\tArnd Bergmann <arnd@arndb.de>
L:\tlinux-arm-kernel@lists.infradead.org (moderated for non-subscribers)
L:\tpvrusb2@isely.net\t(subscribers-only)
S:\tMaintained
F:\tarch/arm/mach-*/
""")
        entry = parse_maintainers(maintainers)['ARM SUB-ARCHITECTURES']
        assert entry.mailing_lists == [
            'linux-arm-kernel@lists.infradead.org',
            'pvrusb2@isely.net',
        ]
        query, _ = build_mailinglist_query(entry, '30.days.ago')
        assert query == '(l:linux-arm-kernel.lists.infradead.org OR l:pvrusb2.isely.net) AND d:30.days.ago..'

    def test_parse_unparsable_list_is_dropped(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """An L: line with no usable address is dropped, with a warning saying so."""
        maintainers = tmp_path / 'MAINTAINERS'
        maintainers.write_text("""
BROKEN ENTRY
M:\tSome One <someone@example.com>
L:\tfoo@lists.example.org (moderated for non-subscribers
L:\tbar@lists.example.org
S:\tMaintained
F:\tdrivers/broken/
""")
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            entry = parse_maintainers(maintainers)['BROKEN ENTRY']
        assert entry.mailing_lists == ['bar@lists.example.org']
        assert 'Ignoring unparsable L: line in BROKEN ENTRY' in caplog.text

    def test_parse_unparsable_maintainer_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """M: and R: lines get the same treatment as L: when they cannot be parsed."""
        maintainers = tmp_path / 'MAINTAINERS'
        maintainers.write_text("""
BROKEN ENTRY
M:\tone@example.com, two@example.com
R:\tReviewer <reviewer@example.com>
S:\tMaintained
F:\tdrivers/broken/
""")
        with caplog.at_level(logging.WARNING, logger='korgalore'):
            entry = parse_maintainers(maintainers)['BROKEN ENTRY']
        assert entry.maintainers == []
        assert entry.reviewers == ['reviewer@example.com']
        assert 'Ignoring unparsable M: line in BROKEN ENTRY' in caplog.text

    def test_parse_all_field_types(self, tmp_path: Path) -> None:
        """Parse all supported field types."""
        maintainers = tmp_path / 'MAINTAINERS'
        # Note: M:/R: lines need angle brackets for email extraction
        maintainers.write_text(
            'TEST SUBSYSTEM\n'
            'M:\tMaintainer Name <maintainer@example.com>\n'
            'M:\tSecond Maintainer <second@example.com>\n'
            'R:\tReviewer Name <reviewer@example.com>\n'
            'L:\tlist@lists.linux.dev\n'
            'S:\tMaintained\n'
            'F:\tpath/to/files/\n'
            'F:\tpath/to/more/\n'
            'X:\tpath/to/excluded/\n'
            'N:\tsimple_pattern\n'
            'K:\tCONFIG_TEST\n'
            'T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/test/test.git\n'
        )
        entries = parse_maintainers(maintainers)
        entry = entries['TEST SUBSYSTEM']
        assert entry.maintainers == ['maintainer@example.com', 'second@example.com']
        assert entry.reviewers == ['reviewer@example.com']
        assert entry.mailing_lists == ['list@lists.linux.dev']
        assert entry.status == 'Maintained'
        assert entry.files == ['path/to/files/', 'path/to/more/']
        assert entry.excluded == ['path/to/excluded/']
        assert entry.file_regex == ['simple_pattern']
        assert entry.content_regex == ['CONFIG_TEST']
        assert entry.trees == [Tree(vcs='git', url='git://git.kernel.org/pub/scm/linux/kernel/git/test/test.git')]

    def test_parse_multiple_trees(self, tmp_path: Path) -> None:
        """Parse subsystem with multiple T: tree entries, one with a branch."""
        maintainers = tmp_path / 'MAINTAINERS'
        maintainers.write_text(
            'MULTI TREE\n'
            'M:\ttest@example.com\n'
            'T:\tgit git://git.kernel.org/pub/scm/linux/kernel/git/test/test.git\n'
            'T:\tgit https://git.kernel.org/pub/scm/linux/kernel/git/test/test-next.git for-next\n'
        )
        entries = parse_maintainers(maintainers)
        entry = entries['MULTI TREE']
        assert entry.trees == [
            Tree(vcs='git', url='git://git.kernel.org/pub/scm/linux/kernel/git/test/test.git'),
            Tree(
                vcs='git',
                url='https://git.kernel.org/pub/scm/linux/kernel/git/test/test-next.git',
                branch='for-next',
            ),
        ]

    def test_parse_layout(self, tmp_path: Path) -> None:
        """Preamble text is ignored, and blank lines between an entry's fields do not end it."""
        maintainers = tmp_path / 'MAINTAINERS'
        maintainers.write_text("""This is preamble text
that should be ignored
until we reach a subsystem title.

FIRST SUBSYSTEM
M:\tfirst@example.com
F:\tfirst/

SECOND SUBSYSTEM

M:\tsecond@example.com

F:\tsecond/
""")
        entries = parse_maintainers(maintainers)
        assert list(entries) == ['FIRST SUBSYSTEM', 'SECOND SUBSYSTEM']
        assert entries['FIRST SUBSYSTEM'].maintainers == ['first@example.com']
        assert entries['FIRST SUBSYSTEM'].files == ['first/']
        assert entries['SECOND SUBSYSTEM'].maintainers == ['second@example.com']
        assert entries['SECOND SUBSYSTEM'].files == ['second/']


@pytest.fixture(scope='module')
def shared_maintainers(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp('maintainers') / 'MAINTAINERS'
    path.write_text(
        'TEST SUBSYSTEM\nM:\ttest@example.com\n\n'
        'ARM/ALLWINNER SOC SUPPORT\nM:\tallwinner@example.com\n\n'
        'INTEL GPU DRIVER\nM:\tintel@example.com\n\n'
        '802.11 WIRELESS NETWORKING\nM:\twireless@example.com\n\n'
        '802.11 BLUETOOTH DRIVER\nM:\tbluetooth@example.com\n\n'
        'GPU\nM:\tgpu@example.com\n\n'
        'GPU MEMORY MANAGER\nM:\tgpumem@example.com\n\n'
        'REGISTER MAP ABSTRACTION LAYER\n'
        'M:\tbroonie@kernel.org\n'
        'L:\tlinux-kernel@vger.kernel.org\n'
        'F:\tdrivers/base/regmap/\n'
    )
    return path


class TestGetSubsystem:
    """Tests for get_subsystem function."""

    @pytest.mark.parametrize(
        ('query', 'name'),
        [
            pytest.param('TEST SUBSYSTEM', 'TEST SUBSYSTEM', id='exact'),
            pytest.param('test subsystem', 'TEST SUBSYSTEM', id='case-insensitive'),
            pytest.param('ALLWINNER', 'ARM/ALLWINNER SOC SUPPORT', id='unique-substring'),
            pytest.param('allwinner', 'ARM/ALLWINNER SOC SUPPORT', id='substring-case-insensitive'),
            # "GPU" is also a substring of two other entries
            pytest.param('GPU', 'GPU', id='exact-preferred-over-substring'),
            # Regression: track-subsystem --forget must use the same normalised key as
            # track-subsystem when creating config, so a substring must resolve to the
            # full canonical entry name, not the user input.
            pytest.param('REGISTER MAP', 'REGISTER MAP ABSTRACTION LAYER', id='substring-resolves-to-canonical-name'),
        ],
    )
    def test_match(self, shared_maintainers: Path, query: str, name: str) -> None:
        assert get_subsystem(shared_maintainers, query).name == name

    @pytest.mark.parametrize(
        ('query', 'exc', 'match'),
        [
            pytest.param('NONEXISTENT', KeyError, 'not found', id='not-found'),
            pytest.param('802.11', ValueError, 'Ambiguous.*matches 2 entries', id='ambiguous-substring'),
        ],
    )
    def test_no_single_match(self, shared_maintainers: Path, query: str, exc: Type[Exception], match: str) -> None:
        with pytest.raises(exc, match=match):
            get_subsystem(shared_maintainers, query)


def generate(tmp_path: Path, **overrides: Any) -> str:
    kwargs: Dict[str, Any] = {
        'key': 'test',
        'target': 'personal',
        'labels': ['INBOX'],
        'lei_base_path': tmp_path / 'lei',
        'since': '30.days.ago',
        'subsystem_name': 'TEST',
    }
    return generate_subsystem_config(**{**kwargs, **overrides})


class TestGenerateSubsystemConfig:
    """Tests for generate_subsystem_config function."""

    @pytest.fixture(scope='class')
    @classmethod
    def lei_base(cls, tmp_path_factory: pytest.TempPathFactory) -> Path:
        return tmp_path_factory.mktemp('gen') / 'lei'

    @pytest.fixture(scope='class')
    @classmethod
    def content(cls, lei_base: Path) -> str:
        return generate_subsystem_config(
            key='9p_file_system',
            target='my_target',
            labels=['INBOX', 'UNREAD', 'IMPORTANT'],
            lei_base_path=lei_base,
            since='30.days.ago',
            subsystem_name='9P FILE SYSTEM',
        )

    def test_valid_toml(self, content: str) -> None:
        config = tomllib.loads(content)
        for section in ('feeds', 'deliveries'):
            assert '9p_file_system-mailinglist' in config[section]
            assert '9p_file_system-patches' in config[section]
        assert config['subsystem']['name'] == '9P FILE SYSTEM'

    def test_text(self, content: str, lei_base: Path) -> None:
        assert "labels = ['INBOX', 'UNREAD', 'IMPORTANT']" in content
        assert "# Auto-generated by: kgl track-subsystem '9P FILE SYSTEM'" in content
        assert '# Query date range: d:30.days.ago..' in content
        assert "target = 'my_target'" in content
        assert f"url = 'lei:{lei_base}/9p_file_system-mailinglist'" in content
        assert f"url = 'lei:{lei_base}/9p_file_system-patches'" in content

    @pytest.mark.parametrize(
        ('include', 'gone', 'kept'),
        [
            pytest.param({'include_mailinglist': False}, 'test-mailinglist', 'test-patches', id='no-mailinglist'),
            pytest.param({'include_patches': False}, 'test-patches', 'test-mailinglist', id='no-patches'),
        ],
    )
    def test_excluded_feed(self, tmp_path: Path, include: Dict[str, bool], gone: str, kept: str) -> None:
        config = tomllib.loads(generate(tmp_path, **include))
        for section in ('feeds', 'deliveries'):
            assert gone not in config.get(section, {})
            assert kept in config[section]
