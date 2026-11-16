"""Czech audio first, otherwise the best verified original with Czech subtitles."""
from datetime import UTC, datetime
import time

from .episodes import display_name
from .models import Episode
from .quality import QUALITY_POLICY, AUTO_SUBTITLE_POLICY, upload_eligible
from .resilience import error_evidence
from .subtitle_policy import discover_subtitled


def inspect_episode(provider, task):
    from .replenish import valid_czech, PREPARATION_REVISION, AUDIO_POLICY_REVISION
    from .search_health import SearchUnavailable
    from .requested_series import PROBE_REVISION, HINT_PROBE_REVISION
    metadata, saved, previous, known = task
    episode = Episode.from_dict(metadata)
    repairing = bool(previous.get('repair_fingerprint'))
    record = dict(at=datetime.now(UTC).isoformat(), status='deferred',
                  method='source_repair' if repairing else 'discovery',
                  preparation_revision=PREPARATION_REVISION, audio_policy_revision=AUDIO_POLICY_REVISION,
                  czech_fallback_revision=1)
    if metadata.get('requested_probe'):
        record['request_probe_revision'] = PROBE_REVISION
    if metadata.get('requested_hint_probe'):
        record['request_hint_probe_revision'] = HINT_PROBE_REVISION
    started = time.monotonic()
    replacement = None
    fatal = False
    try:
        # A current full search is required before authorizing any foreign audio.
        provider.last_czech_absence = None
        candidate = provider.discover_fresh(episode)
        if candidate:
            record.update(observed_audio_language=candidate.audio_language,
                          observed_language_probability=candidate.language_probability,
                          observed_source_id=candidate.source_id)
        if not candidate or not valid_czech(episode, candidate):
            absence = provider.last_czech_absence
            candidate = discover_subtitled(provider, episode, general=True) if absence else None
        else:
            absence = None
        if candidate:
            replacement = dict(identity=episode.identity, episode=episode.to_dict(),
                selected=candidate.to_dict(), display_name=display_name(episode, candidate),
                quality_policy=QUALITY_POLICY, imdb_rating=metadata.get('imdb_rating'),
                imdb_votes=int(metadata.get('imdb_votes') or 0))
            if not valid_czech(episode, candidate):
                subtitles = getattr(candidate, '_subtitle_evidence', {})
                replacement.update(source_policy=AUTO_SUBTITLE_POLICY, subtitles=subtitles,
                                   language_selection=absence)
                if subtitles.get('method') != 'burned_in_ocr_verified':
                    replacement['display_name'] += ' (titulky čekají)'
            if not upload_eligible(replacement):
                raise ValueError('Source lacks complete Czech audio or subtitle proof')
            if metadata.get('requested_probe') or metadata.get('requested_hint_probe'):
                replacement['requested_audio_check'] = dict(revision=PROBE_REVISION,
                    identity=episode.identity, source_id=candidate.source_id,
                    audio_language=candidate.audio_language, checked_at=datetime.now(UTC).isoformat())
            if getattr(candidate, '_source_exclusions', None):
                replacement['source_exclusions'] = candidate._source_exclusions
        else:
            record['reason'] = provider.last_outcome or 'no_verified_czech_source'
    except Exception as error:
        record['reason'] = 'source_search_unavailable' if isinstance(error, SearchUnavailable) else type(error).__name__
        record['error'] = error_evidence(error)
        fatal = isinstance(error, (TypeError, AttributeError, ImportError))
    record['elapsed_seconds'] = round(time.monotonic() - started, 2)
    if repairing:
        from .source_repair import REPAIR_SEARCH_REVISION
        record['source_repair_revision'] = REPAIR_SEARCH_REVISION
    return record, replacement, fatal
