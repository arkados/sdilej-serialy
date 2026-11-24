from types import SimpleNamespace
from threading import RLock

import pytest
import requests

from sdilej_serialy.managed_target import ManagedTargetSession
from sdilej_serialy.resilience import TargetInventoryUnavailable, TargetPending, transient_http
from sdilej_serialy.target import existing_episode, episode_key
from sdilej_serialy.media import prehrajto


NAME='Series S01E01 - Episode 1080p CZ Dabing'


def setup(rows='', extra=None):
    calls=[]
    response=SimpleNamespace(text='<div id="uploadedVideoListing">'+rows+'</div>',raise_for_status=lambda:None)
    def get(url,**kwargs):
        calls.append((url,kwargs))
        if kwargs.get('params',{}).get('searchPhrase'):
            raise AssertionError('Broken remote search must not be called')
        return response
    raw=SimpleNamespace(get=get,post=object())
    records={'new':dict(target_account='a',episode_key=episode_key(NAME),claim={'worker_id':'one'})}
    records.update(extra or {})
    state=SimpleNamespace(_lock=RLock(),data=dict(initialized_at='yes',pilot_verified_at='yes',episodes=records))
    return ManagedTargetSession(raw,state,'a'),state,calls,response


def listing(video_id,name=NAME):
    return f'<section><h3>{name}</h3><a href="?uploadedVideoListing-videoId={video_id}&amp;do=uploadedVideoListing-deleteVideo">Delete</a></section>'


def test_new_managed_episode_checks_recent_real_listing_without_remote_search():
    session,state,calls,response=setup()
    assert existing_episode(session,NAME) is None
    assert calls[0][1]['params'] == {'selectedFolderId': '-1'}
    assert session.post is session.session.post


def test_sdk_confirmation_reads_real_recent_row_for_the_allocated_target():
    session,state,calls,response=setup(listing('123'))
    state.data['episodes']['new']['prepared_target']=dict(target_video_id='123')
    assert prehrajto.uploaded_video_id_by_name(session,NAME)=='123'
    assert session.get(prehrajto.BASE_URL+'/profil/nahrana-videa',params={'searchPhrase':NAME}) is response


@pytest.mark.parametrize('reservation', ['upload','prepared_target','claim'])
def test_older_global_reservations_cannot_be_hidden_by_recent_listing(reservation):
    session,state,calls,response=setup(extra={'old':dict(target_account='b',episode_key=episode_key(NAME),
                                                       **{reservation:{'target_video_id':'999'}})})
    with pytest.raises(TargetPending,match='reserves'):
        existing_episode(session,NAME)


@pytest.mark.parametrize('problem',['unknown','renamed','missing_claim','login_page','old_target'])
def test_unverifiable_recent_listing_fails_closed(problem):
    session,state,calls,response=setup(listing('123'))
    if problem=='renamed':
        state.data['episodes']['other']=dict(target_account='a',episode_key='other:s1:e1',upload=dict(target_video_id='123'))
    elif problem=='missing_claim':state.data['episodes']['new'].pop('claim')
    elif problem=='login_page':response.text='<form>Login</form>'
    elif problem=='old_target':
        response.text='<div id="uploadedVideoListing"></div>'
        state.data['episodes']['new']['prepared_target']=dict(target_video_id='123')
    with pytest.raises(TargetPending) as caught:existing_episode(session,NAME)
    if problem!='old_target':
        assert isinstance(caught.value,TargetInventoryUnavailable) and transient_http(caught.value)


def test_unrelated_reads_and_unverified_generations_are_not_overridden():
    session,state,calls,response=setup()
    session.get(prehrajto.BASE_URL+'/profil/statistiky')
    assert calls[0][0].endswith('/statistiky')
    state.data.pop('pilot_verified_at')
    with pytest.raises(RuntimeError):ManagedTargetSession(session.session,state,'a')


def test_recent_allocation_with_lost_id_remains_quarantined_without_blocking_other_episodes():
    other='Other S02E03 1080p CZ Dabing'
    pending=dict(target_account='a',episode_key=episode_key(other),prepared_target={'creation_intent':True})
    session,state,calls,response=setup(listing('999',other),extra={'uncertain':pending})
    assert existing_episode(session,NAME) is None
    assert state.data['episodes']['uncertain']==pending
    assert 'transfer_receipt' not in pending and pending['prepared_target']=={'creation_intent':True}


def test_portal_removing_ellipsis_from_a_known_title_does_not_block_other_episodes():
    original = 'A jak to bylo dál... S01E05 - Episode 1080p CZ Dabing'
    listed = original.replace('...', '')
    record = dict(target_account='a', episode_key=episode_key(original),
                  prepared_target={'target_video_id': '123'})
    session, state, _, _ = setup(listing('123', listed), extra={'older': record})

    assert existing_episode(session, NAME) is None
    assert state.data['episodes']['older']['episode_key'] == episode_key(original)


def test_ellipsis_normalization_preserves_cross_account_reservations():
    original = 'Series... S01E01 - Episode 1080p CZ Dabing'
    state_row = dict(target_account='b', episode_key=episode_key(original),
                     prepared_target={'target_video_id': '123'})
    session, _, _, _ = setup(extra={'other_account': state_row})

    with pytest.raises(TargetInventoryUnavailable, match='reserves'):
        existing_episode(session, NAME)
