# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, literal, or_
from sqlalchemy.orm import joinedload, noload, selectinload

import horde.classes.base.stats as stats
from horde.bridge_reference import (
    is_backed_validated,
)
from horde.classes.base.waiting_prompt import WPAllowedWorkers, WPModels
from horde.classes.base.worker import WorkerPerformance
from horde.classes.kobold.processing_generation import TextProcessingGeneration

# FIXME: Renamed for backwards compat. To fix later
from horde.classes.kobold.waiting_prompt import TextWaitingPrompt
from horde.classes.kobold.worker import TextWorker, get_minimum_text_worker_speed
from horde.database.functions import (
    POP_CANDIDATE_LIMIT,
    after_pop_candidate,
    pop_candidate_order,
    query_prioritized_wps,
    wp_tricks_worker,
)
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger


# Should be overriden
def convert_things_to_kudos(things, **kwargs):
    # The baseline for a standard generation of 512x512, 50 steps is 10 kudos
    return round(things, 2)


def get_sorted_text_wp_filtered_to_worker(
    worker,
    models_list=None,
    priority_user_ids=None,
    after_candidate=None,
    limit=POP_CANDIDATE_LIMIT,
):
    """Return up to ``limit`` text requests this worker may serve, in pop candidate order.

    Rows are read without locks; a request is claimed only when handed out, by ``WaitingPrompt.start_generation``.
    Priority users' requests come first but are otherwise filtered like any other request.

    Args:
        worker: The popping worker.
        models_list: The models the worker declared on this pop.
        priority_user_ids: The worker owner's id and the ids of its priority users. None means the owner alone.
        after_candidate: The last candidate of the previous read in this pop, or None to read from the start.
        limit: How many candidates to return.
    """
    # This is just the top 3 - Adjusted method to send Worker object. Filters to add.
    # TODO: Filter by (Worker in WP.workers) __ONLY IF__ len(WP.workers) >=1
    # TODO: Filter by WP.trusted_workers == False __ONLY IF__ Worker.user.trusted == False
    # TODO: Filter by Worker not in WP.tricked_worker
    # TODO: If any word in the prompt is in the WP.blacklist rows, then exclude it (L293 in base.worker.Worker.gan_generate())
    slow_speed = get_minimum_text_worker_speed(models_list)
    # The model constraint is a semi-join: joining wp_models returns one row per
    # matching model, and the page LIMIT below counts joined rows, so a WP
    # naming several of the worker's models would consume several page slots as
    # duplicates of itself.
    wp_serves_model = db.session.query(WPModels.id).filter(WPModels.wp_id == TextWaitingPrompt.id, WPModels.model.in_(models_list)).exists()
    wp_names_any_model = db.session.query(WPModels.id).filter(WPModels.wp_id == TextWaitingPrompt.id).exists()
    # Worker targeting is evaluated per WP, never per targeting row: joining
    # wp_allowed_workers admits a blacklisted worker whenever the blacklist
    # names anyone else, because the other rows satisfy a row-level
    # ``worker_id != x`` predicate.
    wp_targets_this_worker = (
        db.session.query(WPAllowedWorkers.id)
        .filter(WPAllowedWorkers.wp_id == TextWaitingPrompt.id, WPAllowedWorkers.worker_id == worker.id)
        .exists()
    )
    wp_has_worker_targets = db.session.query(WPAllowedWorkers.id).filter(WPAllowedWorkers.wp_id == TextWaitingPrompt.id).exists()
    final_wp_list = (
        db.session.query(TextWaitingPrompt)
        .options(
            noload(TextWaitingPrompt.processing_gens),
            # can_generate() reads these for every candidate; loaded lazily that is three SELECTs per candidate per pop.
            selectinload(TextWaitingPrompt.models),
            selectinload(TextWaitingPrompt.tricked_workers),
            selectinload(TextWaitingPrompt.workers),
        )
        .filter(
            TextWaitingPrompt.n > 0,
            TextWaitingPrompt.max_length <= worker.max_length,
            TextWaitingPrompt.max_context_length <= worker.max_context_length,
            TextWaitingPrompt.active == True,  # noqa E712
            TextWaitingPrompt.faulted == False,  # noqa E712
            TextWaitingPrompt.expiry > datetime.utcnow(),
            or_(
                TextWaitingPrompt.safe_ip == True,  # noqa E712
                literal(worker.allow_unsafe_ipaddr),
            ),
            or_(
                TextWaitingPrompt.nsfw == False,  # noqa E712
                literal(worker.nsfw),
            ),
            or_(
                wp_serves_model,
                ~wp_names_any_model,
            ),
            or_(
                ~wp_has_worker_targets,
                and_(
                    TextWaitingPrompt.worker_blacklist.is_(False),
                    wp_targets_this_worker,
                ),
                and_(
                    TextWaitingPrompt.worker_blacklist.is_(True),
                    ~wp_targets_this_worker,
                ),
            ),
            or_(
                literal(worker.speed >= slow_speed),  # Slow speed is based on the model parameters used
                TextWaitingPrompt.slow_workers == True,  # noqa E712
            ),
            or_(
                literal(not worker.maintenance),
                TextWaitingPrompt.user_id == worker.user_id,
            ),
            or_(
                literal(is_backed_validated(worker.bridge_agent)),
                TextWaitingPrompt.validated_backends.is_(False),
            ),
        )
    )
    # Repeats a check of Worker.can_generate, which still runs on every candidate. The text pop reports refusals only
    # from can_generate, so checks with a reported reason stay there rather than move here.
    final_wp_list = final_wp_list.filter(~wp_tricks_worker(TextWaitingPrompt, worker))
    if not priority_user_ids:
        # A worker's owner is a priority user of every pop it makes.
        priority_user_ids = [worker.user_id]
    is_priority_request = TextWaitingPrompt.user_id.in_(priority_user_ids)
    if after_candidate is not None:
        final_wp_list = final_wp_list.filter(
            after_pop_candidate(TextWaitingPrompt, is_priority_request, after_candidate, priority_user_ids),
        )
    final_wp_list = final_wp_list.order_by(*pop_candidate_order(TextWaitingPrompt, is_priority_request)).limit(limit)
    return final_wp_list.populate_existing().all()


def get_text_wp_by_id(wp_id, lite=False):
    try:
        wp_uuid = uuid.UUID(wp_id)
    except ValueError:
        logger.debug(f"Non-UUID wp_id sent: '{wp_id}'.")
        return None
    if SQLITE_MODE:
        wp_uuid = str(wp_uuid)
    # lite version does not pull ProcGens
    if lite:
        query = db.session.query(TextWaitingPrompt).options(noload(TextWaitingPrompt.processing_gens))
    else:
        query = db.session.query(TextWaitingPrompt)
    return query.filter_by(id=wp_uuid).first()


def get_text_progen_by_id(procgen_id):
    try:
        procgen_uuid = uuid.UUID(procgen_id)
    except ValueError:
        logger.debug(f"Non-UUID procgen_id sent: '{procgen_id}'.")
        return None
    if SQLITE_MODE:
        procgen_uuid = str(procgen_uuid)
    # The submit settlement always walks procgen -> wp -> requesting user and
    # procgen -> worker -> owning user, so loading them here folds four lazy
    # SELECT round trips into the lookup itself.
    return (
        db.session.query(TextProcessingGeneration)
        .options(
            joinedload(TextProcessingGeneration.wp).joinedload(TextWaitingPrompt.user),
            joinedload(TextProcessingGeneration.worker).joinedload(TextWorker.user),
        )
        .filter_by(id=procgen_uuid)
        .first()
    )


def get_all_text_wps():
    return (
        db.session.query(TextWaitingPrompt)
        .filter(
            TextWaitingPrompt.active == True,  # noqa E712
            TextWaitingPrompt.faulted == False,  # noqa E712
            TextWaitingPrompt.expiry > datetime.utcnow(),
        )
        .all()
    )


def get_cached_worker_performance():
    if hr.horde_r is None:
        return [p.performance for p in db.session.query(WorkerPerformance.performance).all()]
    perf_cache = hr.horde_r.get("worker_performances_cache")
    if not perf_cache:
        return refresh_worker_performances_cache()
    try:
        models_ret = json.loads(perf_cache)
    except TypeError:
        logger.error("performance cache could not be loaded: {perf_cache}")
        return refresh_worker_performances_cache()
    if models_ret is None:
        return refresh_worker_performances_cache()
    return models_ret


# TODO: Convert below three functions into a general "cached db request" (or something) class
# Which I can reuse to cache the results of other requests
def retrieve_worker_performances():
    avg_perf = db.session.query(func.avg(WorkerPerformance.performance)).scalar()
    avg_perf = 0 if avg_perf is None else round(avg_perf, 2)
    return avg_perf  # noqa RET504


def refresh_worker_performances_cache():
    avg_perf = retrieve_worker_performances()
    try:
        hr.horde_r_setex("worker_performances_avg_cache", timedelta(seconds=30), avg_perf)
    except Exception as e:
        logger.debug(f"Error when trying to set worker performances cache: {e}. Retrieving from DB.")
    return avg_perf


def query_prioritized_text_wps():
    return query_prioritized_wps()


def prune_expired_stats():
    # clear up old requests (older than 5 mins)
    db.session.query(stats.FulfillmentPerformance).filter(
        stats.FulfillmentPerformance.created < datetime.utcnow() - timedelta(seconds=60),
    ).delete(synchronize_session=False)
    db.session.query(stats.ModelPerformance).filter(
        stats.ModelPerformance.created < datetime.utcnow() - timedelta(hours=1),
    ).delete(synchronize_session=False)
    db.session.commit()
    logger.debug("Pruned Expired Stats")
