#!/usr/bin/env python3
"""Tests for per-crawl Persona runtime profile management."""

import json
import textwrap

from .conftest import run_python_cwd


def test_persona_prepare_runtime_for_crawl_clones_and_cleans_profile(initialized_archive):
    script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.core.settings')
        import django
        django.setup()

        from archivebox.crawls.models import Crawl
        from archivebox.personas.models import Persona

        persona, _ = Persona.objects.get_or_create(name='Default')
        persona.ensure_dirs()

        template_dir = Path(persona.CHROME_USER_DATA_DIR)
        (template_dir / 'SingletonLock').write_text('locked')
        (template_dir / 'chrome.log').write_text('noise')
        (template_dir / 'Default' / 'GPUCache').mkdir(parents=True, exist_ok=True)
        (template_dir / 'Default' / 'GPUCache' / 'blob').write_text('cached')
        (template_dir / 'Default' / 'Preferences').write_text('{"ok": true}')

        crawl = Crawl.objects.create(urls='https://example.com', persona_id=persona.id)
        overrides = persona.prepare_runtime_for_crawl(
            crawl,
            chrome_binary='/Applications/Chromium.app/Contents/MacOS/Chromium',
        )

        runtime_root = persona.runtime_root_for_crawl(crawl)
        runtime_profile = Path(overrides['CHROME_USER_DATA_DIR'])
        runtime_downloads = Path(overrides['CHROME_DOWNLOADS_DIR'])

        print(json.dumps({
            'runtime_root_exists': runtime_root.exists(),
            'runtime_profile_exists': runtime_profile.exists(),
            'runtime_downloads_exists': runtime_downloads.exists(),
            'preferences_copied': (runtime_profile / 'Default' / 'Preferences').exists(),
            'singleton_removed': not (runtime_profile / 'SingletonLock').exists(),
            'cache_removed': not (runtime_profile / 'Default' / 'GPUCache').exists(),
            'log_removed': not (runtime_profile / 'chrome.log').exists(),
            'persona_name_recorded': (runtime_root / 'persona_name.txt').read_text().strip(),
            'template_dir_recorded': (runtime_root / 'template_dir.txt').read_text().strip(),
            'chrome_binary_recorded': (runtime_root / 'chrome_binary.txt').read_text().strip(),
        }))
        """,
    )

    stdout, stderr, code = run_python_cwd(script, cwd=initialized_archive, timeout=60)
    assert code == 0, stderr

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["runtime_root_exists"] is True
    assert payload["runtime_profile_exists"] is True
    assert payload["runtime_downloads_exists"] is True
    assert payload["preferences_copied"] is True
    assert payload["singleton_removed"] is True
    assert payload["cache_removed"] is True
    assert payload["log_removed"] is True
    assert payload["persona_name_recorded"] == "Default"
    assert payload["template_dir_recorded"].endswith("/personas/Default/chrome_profile")
    assert payload["chrome_binary_recorded"] == "/Applications/Chromium.app/Contents/MacOS/Chromium"


def test_persona_cleanup_runtime_for_crawl_removes_only_runtime_copy(initialized_archive):
    script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.core.settings')
        import django
        django.setup()

        from archivebox.crawls.models import Crawl
        from archivebox.personas.models import Persona

        persona, _ = Persona.objects.get_or_create(name='Default')
        persona.ensure_dirs()
        template_dir = Path(persona.CHROME_USER_DATA_DIR)
        (template_dir / 'Default').mkdir(parents=True, exist_ok=True)
        (template_dir / 'Default' / 'Preferences').write_text('{"kept": true}')

        crawl = Crawl.objects.create(urls='https://example.com', persona_id=persona.id)
        persona.prepare_runtime_for_crawl(crawl)
        runtime_root = persona.runtime_root_for_crawl(crawl)

        persona.cleanup_runtime_for_crawl(crawl)

        print(json.dumps({
            'runtime_removed': not runtime_root.exists(),
            'template_still_exists': (template_dir / 'Default' / 'Preferences').exists(),
        }))
        """,
    )

    stdout, stderr, code = run_python_cwd(script, cwd=initialized_archive, timeout=60)
    assert code == 0, stderr

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["runtime_removed"] is True
    assert payload["template_still_exists"] is True


def test_crawl_runner_respects_chrome_isolation_config(initialized_archive):
    script = textwrap.dedent(
        """
        import json
        import os

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.core.settings')
        import django
        django.setup()

        from archivebox.crawls.models import Crawl
        from archivebox.services.runner import CrawlRunner

        crawl_default = Crawl.objects.create(urls='https://example.com')
        runner_default = CrawlRunner(crawl_default)
        runner_default.load_run_state()

        crawl_snapshot = Crawl.objects.create(
            urls='https://example.com/explicit',
            config={'CHROME_ISOLATION': 'snapshot'},
        )
        runner_snapshot = CrawlRunner(crawl_snapshot)
        runner_snapshot.load_run_state()

        print(json.dumps({
            'default_isolation': runner_default.base_config.get('CHROME_ISOLATION'),
            'explicit_isolation': runner_snapshot.base_config.get('CHROME_ISOLATION'),
        }))
        """,
    )

    stdout, stderr, code = run_python_cwd(script, cwd=initialized_archive, timeout=60)
    assert code == 0, stderr

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["default_isolation"] == "crawl"
    assert payload["explicit_isolation"] == "snapshot"


def test_crawl_resolve_persona_raises_for_missing_persona_id(initialized_archive):
    script = textwrap.dedent(
        """
        import json
        import os
        from uuid import uuid4

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.core.settings')
        import django
        django.setup()

        from archivebox.crawls.models import Crawl
        from archivebox.personas.models import Persona

        crawl = Crawl.objects.create(urls='https://example.com', persona_id=uuid4())

        try:
            crawl.resolve_persona()
        except Persona.DoesNotExist as err:
            print(json.dumps({'raised': True, 'message': str(err)}))
        else:
            raise SystemExit('resolve_persona unexpectedly succeeded')
        """,
    )

    stdout, stderr, code = run_python_cwd(script, cwd=initialized_archive, timeout=60)
    assert code == 0, stderr

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["raised"] is True
    assert "references missing Persona" in payload["message"]


def test_get_config_raises_for_missing_persona_id(initialized_archive):
    script = textwrap.dedent(
        """
        import json
        import os
        from uuid import uuid4

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.core.settings')
        import django
        django.setup()

        from archivebox.config.common import get_config
        from archivebox.crawls.models import Crawl
        from archivebox.personas.models import Persona

        crawl = Crawl.objects.create(urls='https://example.com', persona_id=uuid4())

        try:
            get_config(crawl=crawl)
        except Persona.DoesNotExist as err:
            print(json.dumps({'raised': True, 'message': str(err)}))
        else:
            raise SystemExit('get_config unexpectedly succeeded')
        """,
    )

    stdout, stderr, code = run_python_cwd(script, cwd=initialized_archive, timeout=60)
    assert code == 0, stderr

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["raised"] is True
    assert "references missing Persona" in payload["message"]


def test_get_config_resolves_parent_scopes_when_only_archiveresult_is_passed(initialized_archive):
    script = textwrap.dedent(
        """
        import json
        import os

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'archivebox.core.settings')
        os.environ['TIMEOUT'] = '22'
        os.environ['CHROME_BINARY'] = 'env-chrome'

        import django
        django.setup()

        from archivebox.config import CONSTANTS
        from archivebox.config.common import get_config
        from archivebox.core.models import ArchiveResult, Snapshot
        from archivebox.crawls.models import Crawl
        from archivebox.machine.models import Machine
        from archivebox.personas.models import Persona

        CONSTANTS.CONFIG_FILE.write_text('[ARCHIVING_CONFIG]\\nTIMEOUT=11\\nCHROME_BINARY=file-chrome\\n')

        machine = Machine.current()
        machine.config = {'CHROME_BINARY': 'machine-chrome'}
        machine.save(update_fields=['config'])

        persona = Persona.objects.create(
            name='StackPersona',
            config={'TIMEOUT': 33, 'CHROME_BINARY': 'persona-chrome'},
        )
        persona.ensure_dirs()
        crawl = Crawl.objects.create(
            urls='https://example.com',
            persona_id=persona.id,
            config={'TIMEOUT': 44, 'CHROME_BINARY': 'crawl-chrome'},
        )
        snapshot = Snapshot.objects.create(
            url='https://example.com',
            crawl=crawl,
            config={'TIMEOUT': 55, 'CHROME_BINARY': 'snapshot-chrome'},
        )
        result = ArchiveResult.objects.create(
            snapshot=snapshot,
            plugin='title',
            config={'TIMEOUT': 66, 'CHROME_BINARY': 'archiveresult-chrome'},
        )

        env_config = get_config(include_machine=False)
        machine_config = get_config(machine=machine)
        persona_config = get_config(persona=persona)
        crawl_config = get_config(crawl=crawl)
        snapshot_config = get_config(snapshot=snapshot)
        result_config = get_config(archiveresult=result)
        override_config = get_config(archiveresult=result, overrides={'TIMEOUT': 77, 'CHROME_BINARY': 'override-chrome'})

        print(json.dumps({
            'env': [env_config.TIMEOUT, env_config.CHROME_BINARY],
            'machine': [machine_config.TIMEOUT, machine_config.CHROME_BINARY],
            'persona': [persona_config.TIMEOUT, persona_config.CHROME_BINARY],
            'crawl': [crawl_config.TIMEOUT, crawl_config.CHROME_BINARY],
            'snapshot': [snapshot_config.TIMEOUT, snapshot_config.CHROME_BINARY],
            'archiveresult': [result_config.TIMEOUT, result_config.CHROME_BINARY],
            'override': [override_config.TIMEOUT, override_config.CHROME_BINARY],
            'snap_dir': str(result_config.SNAP_DIR),
            'expected_snap_dir': str(snapshot.output_dir),
            'crawl_dir': str(result_config.CRAWL_DIR),
            'expected_crawl_dir': str(crawl.output_dir),
            'active_persona': result_config.ACTIVE_PERSONA,
        }, default=str))
        """,
    )

    stdout, stderr, code = run_python_cwd(script, cwd=initialized_archive, timeout=60)
    assert code == 0, stderr

    payload = json.loads(stdout.strip().splitlines()[-1])
    assert payload["env"] == [22, "env-chrome"]
    assert payload["machine"] == [22, "machine-chrome"]
    assert payload["persona"] == [33, "persona-chrome"]
    assert payload["crawl"] == [44, "crawl-chrome"]
    assert payload["snapshot"] == [55, "snapshot-chrome"]
    assert payload["archiveresult"] == [66, "archiveresult-chrome"]
    assert payload["override"] == [77, "override-chrome"]
    assert payload["snap_dir"] == payload["expected_snap_dir"]
    assert payload["crawl_dir"] == payload["expected_crawl_dir"]
    assert payload["active_persona"] == "StackPersona"
