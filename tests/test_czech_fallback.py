from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import gzip
import hashlib
import json
from types import SimpleNamespace

import pytest

from sdilej_serialy import czech_fallback, episode_subtitles, quality, subtitle_policy
from sdilej_serialy.episodes import display_name
from sdilej_serialy.media.models import Candidate, LanguageTier, MatchTier
from sdilej_serialy.migration import audit, targets
from sdilej_serialy.models import Episode
from sdilej_serialy.source_repair import repair_entry, repaired_row, repairable
from sdilej_serialy.subtitle_media import SubtitleUnavailable


def fixture():
    episode = Episode(1, 99, 'Český název', 'The Series', 1, 1, runtime_min=20, original_language='en')
    candidate = Candidate('123', 'https://sdilej.cz/123/series.mkv', 'The Series S01E01',
                          duration_sec=1200, size_bytes=300000000, width=1920, height=1080,
                          audio_language='en', language_probability=.99, language_tier=LanguageTier.FOREIGN_AUDIO,
                          match_tier=MatchTier.STRONG, download_url='https://private.invalid/?token=private')
    absence = dict(policy='complete-czech-search-v1', identity=episode.identity,
                   czech_search_complete=True, czech_found=False, checked_at=datetime.now(UTC).isoformat(),
                   checked_source_count=3, checked_sources_sha256='a' * 64)
    payload = ''.join(f'{i}\n00:00:{i:02d},000 --> 00:19:{i:02d},000\nČeský text {i}\n\n'
                      for i in range(1, 21)).encode()
    candidate._subtitle_evidence = subtitle_policy.subtitle_evidence(payload, candidate, {})
    row = dict(identity=episode.identity, episode=episode.to_dict(), selected=candidate.to_dict(),
               display_name=display_name(episode, candidate), quality_policy=quality.QUALITY_POLICY,
               source_policy=quality.AUTO_SUBTITLE_POLICY, subtitles=candidate._subtitle_evidence,
               language_selection=absence)
    return episode, candidate, absence, row, payload


def test_native_czech_has_no_dubbing_suffix_and_foreign_dubbing_keeps_czech_names():
    ep, candidate, *_ = fixture()
    cz = replace(candidate, audio_language='cs', language_tier=LanguageTier.CZECH_AUDIO)
    assert display_name(ep, cz) == 'Český název S01E01 1080p CZ Dabing'
    assert display_name(replace(ep, original_language='cs'), cz) == 'Český název S01E01 1080p'


def test_any_series_can_use_exact_original_with_complete_czech_text():
    ep, candidate, absence, row, payload = fixture()
    assert quality.upload_eligible(row)
    assert quality.current_upload_eligible(row)
    row['episode']['original_language'] = 'ja'
    row['selected']['audio_language'] = 'ja'
    assert quality.upload_eligible(row)
    row['selected']['audio_language'] = 'en'
    assert not quality.upload_eligible(row)


@pytest.mark.parametrize('corruption', ['absence', 'incomplete_search', 'source', 'size', 'forced', 'short', 'unknown_audio', 'wrong_episode'])
def test_fallback_requires_czech_absence_and_exact_episode_subtitle_binding(corruption):
    _, _, _, row, _ = fixture()
    if corruption == 'absence':
        row['language_selection'] = {}
    elif corruption == 'incomplete_search':
        row['language_selection']['czech_search_complete'] = False
    elif corruption == 'source':
        row['subtitles']['source_id'] = '999'
    elif corruption == 'size':
        row['subtitles']['source_size_bytes'] += 1
    elif corruption == 'forced':
        row['subtitles']['full'] = False
    elif corruption == 'short':
        row['subtitles']['cue_count'] = 2
    elif corruption == 'unknown_audio':
        row['selected']['language_probability'] = .4
    else:
        row['selected']['title'] = 'The Series S01E02'
    assert not quality.upload_eligible(row)


def test_czech_audio_precedes_a_higher_resolution_foreign_subtitled_source(monkeypatch):
    ep, candidate, absence, row, _ = fixture()
    cz = replace(candidate, audio_language='cs', language_tier=LanguageTier.CZECH_AUDIO, width=1280, height=720)
    provider = SimpleNamespace(discover_fresh=lambda _: cz, last_outcome='verified_czech')
    monkeypatch.setattr(czech_fallback, 'discover_subtitled', lambda *_args, **_kw: pytest.fail('Czech audio was already found'))
    record, result, fatal = czech_fallback.inspect_episode(provider, (ep.to_dict(), None, {}, []))
    assert not fatal and result['selected']['audio_language'] == 'cs'
    assert result['display_name'].endswith('720p CZ Dabing')


def test_incomplete_czech_search_never_authorizes_foreign_fallback(monkeypatch):
    ep, candidate, _, _, _ = fixture()
    provider = SimpleNamespace(discover_fresh=lambda _: None, last_outcome='transient_original')
    monkeypatch.setattr(czech_fallback, 'discover_subtitled', lambda *_args, **_kw: pytest.fail('Czech search incomplete'))
    _, result, fatal = czech_fallback.inspect_episode(provider, (ep.to_dict(), None, {}, []))
    assert result is None and not fatal


def test_verified_fallback_keeps_pending_suffix_only_until_text_is_attached(monkeypatch):
    ep, candidate, absence, row, _ = fixture()
    provider = SimpleNamespace(last_outcome='verified_non_czech')
    def discover(_):
        provider.last_czech_absence = absence
        return candidate
    provider.discover_fresh = discover
    monkeypatch.setattr(czech_fallback, 'discover_subtitled', lambda *_args, **_kw: candidate)
    _, result, fatal = czech_fallback.inspect_episode(provider, (ep.to_dict(), None, {}, []))
    assert not fatal and quality.upload_eligible(result)
    assert result['display_name'].endswith('CZ titulky (titulky čekají)')
    assert 'download_url' not in result['selected'] and 'private' not in json.dumps(result)


def burned_proof(candidate):
    return dict(language='cs', full=True, method='burned_in_ocr_verified', source_id=candidate.source_id,
        source_url=candidate.url, source_size_bytes=candidate.size_bytes, visual_policy='burned-in-czech-v1',
        visual_status='czech_burned_in_verified', visual_language='cs', visual_language_confidence=.99,
        visual_text_samples=6, visual_distinct_lines=6,
        visual_frame_sha256=[hashlib.sha256(str(i).encode()).hexdigest() for i in range(6)])


def test_burned_in_czech_is_verified_from_original_pixels_without_a_subtitle_upload(monkeypatch):
    ep, candidate, absence, row, _ = fixture()
    proof = burned_proof(candidate)
    provider = SimpleNamespace(discovery_timeout_seconds=60, session=None, search=lambda _: [candidate],
                               _inspect=lambda *_: candidate, _verify_language=lambda *_: candidate,
                               visual_inspector=lambda *_args, **_kw: proof)
    def missing_text(*_args, **_kw):
        raise SubtitleUnavailable('source_czech_text_missing')
    monkeypatch.setattr(subtitle_policy, 'extract_original', missing_text)
    result = subtitle_policy.discover_subtitled(provider, ep, general=True)
    row['subtitles'] = result._subtitle_evidence
    assert quality.upload_eligible(row)
    row['target_account'] = 'a'
    uploads = {'episodes': {ep.identity: {'target_account': 'a', 'upload': {'target_video_id': '777'}}}}
    assert list(episode_subtitles.eligible_rows([row], uploads, {})) == []
    row['subtitles']['visual_language'] = 'sk'
    assert not quality.upload_eligible(row)


def test_subtitled_repair_keeps_frozen_account_rank_and_episode_identity():
    ep, candidate, absence, replacement, _ = fixture()
    base = deepcopy(replacement)
    base.pop('source_policy'); base.pop('subtitles'); base.pop('language_selection')
    base['selected'].update(audio_language='cs', language_tier='czech_audio')
    base.update(target_account='b', queue_rank=6)
    plan = dict(generation='test', manifest_sha256='b' * 64)
    proof = repair_entry(base, replacement, plan)
    actual = repaired_row(base, proof, plan)
    assert actual['target_account'] == 'b' and actual['queue_rank'] == 6
    assert actual['identity'] == ep.identity and quality.upload_eligible(actual)


def test_policy_review_cannot_replace_an_allocated_or_uploaded_source():
    assert repairable({'language_policy_review': True})
    assert not repairable({'language_policy_review': True, 'prepared_target': {'creation_intent': True}})
    assert not repairable({'language_policy_review': True, 'upload': {'target_video_id': '777'}})


def test_history_audit_preserves_target_ids_when_pending_becomes_completed(tmp_path, monkeypatch):
    from test_dual import setup_plan
    directory, plan, rows = setup_plan(tmp_path, monkeypatch)
    path = directory / 'state.json'
    state = json.loads(path.read_text())
    state['episodes'][rows[0]['identity']] = dict(target_account='a', prepared_target={'target_video_id': '777'})
    ids, _ = targets(state)
    baseline = dict(generation='test', account_hashes=plan['account_hashes'],
                    manifest_sha256=plan['manifest_sha256'], account_statistics={'a': 1, 'b': 0}, targets=ids)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/migration-baseline.json.gz').write_bytes(gzip.compress(json.dumps(baseline).encode()))
    state['episodes'][rows[0]['identity']] = dict(target_account='a', upload={'target_video_id': '777'})
    path.write_text(json.dumps(state))
    assert audit(tmp_path, 'test')['preserved_target_ids'] == 1
    state['episodes'][rows[0]['identity']]['upload']['target_video_id'] = '999'
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='lost or moved'):
        audit(tmp_path, 'test')


def test_missing_old_detail_is_excluded_only_after_two_confirmations(monkeypatch):
    from sdilej_serialy.episodes import EpisodeSourceProvider
    from sdilej_serialy.source_detail import MissingSourceDetail
    ep, candidate, *_ = fixture()
    old = replace(candidate, source_id='999', width=3840, height=2160)
    current = replace(candidate, audio_language='cs', language_tier=LanguageTier.CZECH_AUDIO)
    provider = EpisodeSourceProvider(None, detector=object())
    monkeypatch.setattr(provider, 'search', lambda _: [old, current])
    checks = []
    def inspect(_ep, c):
        if c.source_id == '999':
            checks.append(c.source_id)
            raise MissingSourceDetail(c.source_id)
        return c
    monkeypatch.setattr(provider, '_inspect', inspect)
    monkeypatch.setattr(provider, '_verify_language', lambda _ep, c: c)
    result = provider.discover(ep)
    assert result.source_id == '123' and checks == ['999', '999']
    assert result._source_exclusions[0]['reason'] == 'source_detail_unavailable'


def test_new_subtitled_reserve_is_published_without_touching_upload_history(tmp_path, monkeypatch):
    from sdilej_serialy import replenish, dual
    from sdilej_serialy.pipeline import atomic_json
    from test_replenish import setup, discover
    directory, plan, rows = setup(tmp_path, monkeypatch)
    before = (directory / 'state.json').read_bytes()
    atomic_json(directory / 'uploader-capabilities.json', dict(schema_version=1, subtitle_queue_version=2,
                generation='test', source_policies=[quality.AUTO_SUBTITLE_POLICY]))
    provider = SimpleNamespace(czech_subtitle_fallback=True, last_outcome='verified_non_czech')
    def search(ep):
        c = replace(discover(ep), audio_language='en', language_tier=LanguageTier.FOREIGN_AUDIO)
        provider.last_czech_absence = dict(policy='complete-czech-search-v1', identity=ep.identity,
            czech_search_complete=True, czech_found=False, checked_at=datetime.now(UTC).isoformat(),
            checked_source_count=1, checked_sources_sha256='c' * 64)
        c._subtitle_evidence = dict(language='cs', full=True, source_id=c.source_id, source_url=c.url,
            source_size_bytes=c.size_bytes, cue_count=30, end_ms=c.duration_sec * 900, sha256='d' * 64)
        provider.candidate = c
        return c
    provider.discover_fresh = search
    monkeypatch.setattr(czech_fallback, 'discover_subtitled', lambda *_args, **_kw: provider.candidate)
    result = replenish.prepare(tmp_path, 'test', provider, limit=2, runtime_minutes=0)
    assert result['prepared_this_run'] == 2
    assert (directory / 'state.json').read_bytes() == before
    _, _, after = dual.load(tmp_path, 'test')
    assert [r['target_account'] for r in after[4:]] == ['a', 'b']
    assert all(r['source_policy'] == quality.AUTO_SUBTITLE_POLICY for r in after[4:])


def test_subtitle_lookup_uses_owned_id_and_unfiltered_pages():
    import time
    from sdilej_serialy.subtitle_target import Target
    upload = {str(n): {'target_account': 'a', 'upload': {'target_video_id': str(n)}} for n in range(1, 41)}
    wanted = Target('15', 'Český název S01E01 1080p', True)
    calls = []
    def listing(page):
        calls.append(page)
        if page == 3:
            return [wanted], 4
        return [Target(str(n), 'Other S01E01', True) for n in range(31, 41)], 4
    api = SimpleNamespace(listing=listing)
    entry = {}
    actual = episode_subtitles.owned_listing(api, '15', 'Český název S01E01',
        {'episodes': upload}, 'a', entry, deadline=time.monotonic() + 300)
    assert actual.video_id == '15' and entry['listing_page'] == 3
    assert calls == [1, 3]


@pytest.mark.parametrize('language', ['cs', 'en'])
def test_requested_series_advances_after_a_verified_czech_accessible_sample(tmp_path, monkeypatch, language):
    from sdilej_serialy.requested_series import RequestedSeries, PROBE_REVISION
    ep, candidate, absence, row, _ = fixture()
    if language == 'cs':
        candidate = replace(candidate, audio_language='cs', language_tier=LanguageTier.CZECH_AUDIO)
    provider = SimpleNamespace(last_outcome='verified_non_czech')
    def discover(_):
        provider.last_czech_absence = absence
        return candidate
    provider.discover_fresh = discover
    monkeypatch.setattr(czech_fallback, 'discover_subtitled', lambda *_args, **_kw: candidate)
    metadata = dict(ep.to_dict(), requested_probe=True)
    record, selected, fatal = czech_fallback.inspect_episode(provider, (metadata, None, {}, []))
    assert not fatal and selected and record['request_probe_revision'] == PROBE_REVISION
    path = tmp_path / 'requested.json'
    path.write_text(json.dumps(dict(schema_version=1, generation='test', series=[dict(series_id=99,
        title='Český název', episodes=[dict(season=1, episode=n) for n in range(1, 5)])])))
    requested = RequestedSeries(path, 'test', {ep.identity: record}, {ep.identity: selected})
    assert requested.positive(99, 1)
    assert requested.available(replace(ep, number=4).to_dict())


def test_old_pending_targets_cannot_fill_the_new_upload_batch(tmp_path, monkeypatch):
    from test_dual import setup_plan
    from sdilej_serialy.dual import SharedState, account_batch
    directory, plan, rows = setup_plan(tmp_path, monkeypatch, count=40)
    state = SharedState(directory / 'state.json', {r['identity']: r['target_account'] for r in rows})
    allocated = [r for r in rows if r['target_account'] == 'a'][:10]
    for row in allocated:
        state.data['episodes'][row['identity']] = dict(target_account='a',
            prepared_target={'target_video_id': str(10000 + row['queue_rank'])})
    result = account_batch(rows, state, set(), 'a', 5)
    assert len(result) == 5
    assert sum(row in allocated for row in result) == 1
    assert sum(row not in allocated for row in result) == 4


def test_frozen_native_czech_backlog_gets_the_correct_new_label_without_rewriting_its_row():
    from sdilej_serialy.episodes import native_czech_name
    ep, candidate, absence, row, _ = fixture()
    row['episode'].pop('original_language')
    row['selected'].update(audio_language='cs', language_tier='czech_audio')
    row['display_name'] = 'Český název S01E01 1080p CZ Dabing'
    original = deepcopy(row)
    result = native_czech_name(row, {'99': {'status': 'verified', 'original_language': 'cs'}})
    assert result['display_name'] == 'Český název S01E01 1080p'
    assert result['identity'] == row['identity'] and result['selected'] == row['selected']
    assert row == original
