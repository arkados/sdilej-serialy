"""Unfiltered target reads for a durably managed, initially empty generation."""
from .resilience import TargetInventoryUnavailable, TargetPending
from .target import episode_key, listing_rows, target_episode_key
from sdilej_serialy.media import prehrajto


class ManagedTargetSession:
    """Keep SDK POSTs unchanged; avoid the target's broken search endpoint.

    Global claims/history are authoritative for this generation's older videos.
    Recent target rows additionally detect out-of-band changes. A missing older
    allocation remains pending and is confirmed through its owned ID by the
    caller, never by inventing a listing row or repeating its POST.
    """

    def __init__(self, session, state, account):
        if not state.data.get('initialized_at') or not state.data.get('pilot_verified_at'):
            raise RuntimeError('Managed listing requires a verified initially empty generation')
        self.session, self.state, self.account = session, state, account
        self.logged = False

    def __getattr__(self, name):
        return getattr(self.session, name)

    def get(self, url, **kwargs):
        params = kwargs.get('params') or {}
        if url != prehrajto.BASE_URL + '/profil/nahrana-videa' or not params.get('searchPhrase'):
            return self.session.get(url, **kwargs)
        key = target_episode_key(episode_key(params['searchPhrase']))
        if not key:
            raise TargetInventoryUnavailable('Managed target read requires an exact episode identity')
        options = dict(kwargs, params={k:v for k,v in params.items() if k != 'searchPhrase'})
        options['params'].setdefault('selectedFolderId', '-1')
        response = self.session.get(url, **options)
        response.raise_for_status()
        if 'uploadedVideoListing' not in response.text:
            raise TargetInventoryUnavailable('Recent owned target listing is unavailable')
        recent = listing_rows(response.text)
        with self.state._lock:
            records = self.state.data['episodes']
            def record_key(r):
                return target_episode_key(
                    r.get('episode_key') or episode_key((r.get('upload') or {}).get('display_name', ''))
                    or episode_key((r.get('source') or {}).get('display_name', ''))
                )
            matching = [(i,r) for i,r in records.items() if record_key(r) == key]
            claims = [(i,r) for i,r in matching if r.get('claim') and r.get('target_account') == self.account]
            if len(claims) != 1:
                raise TargetInventoryUnavailable('Managed target lookup requires one exclusive owned claim')
            identity, current = claims[0]
            if any(i != identity and (r.get('upload') or r.get('prepared_target') or r.get('claim'))
                   for i,r in matching):
                raise TargetInventoryUnavailable('Another managed record reserves this episode')
            owned = {}
            reserved_intents = set()
            for r in records.values():
                if r.get('target_account') != self.account:
                    continue
                prepared = r.get('prepared_target') or {}
                if prepared.get('creation_intent') is True and not prepared.get('target_video_id'):
                    reserved_intents.add(record_key(r))
                for target in (r.get('upload') or {}, r.get('prepared_target') or {}):
                    if target.get('target_video_id'):
                        owned[str(target['target_video_id'])] = record_key(r)
            for row in recent:
                listed_key = target_episode_key(row['key'])
                if row['id'] not in owned and listed_key in reserved_intents:
                    # The allocation response may be lost or its checkpoint
                    # still in flight. Keep its episode quarantined; do not
                    # invent an ID/receipt or block unrelated managed episodes.
                    continue
                if row['id'] not in owned or listed_key != owned[row['id']]:
                    raise TargetInventoryUnavailable('Recent target listing contains an untracked or renamed video')
            known = current.get('upload') or current.get('prepared_target') or {}
            if known.get('target_video_id') and str(known['target_video_id']) not in {r['id'] for r in recent}:
                raise TargetPending('Older allocated target requires owned-ID confirmation')
        if not self.logged:
            print('target_listing_mode=managed_history_and_unfiltered_recent', flush=True)
            self.logged = True
        return response
