# SPDX-FileCopyrightText: 2026 Tazlin
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Record which accounts share networks, restrict registrations from them, and report transfers between linked accounts.

Two accounts are linked when both were seen on the same IP subject (an IPv4 address or an IPv6 /64 network) within
``AccountLinkWindows.RETENTION``. A link is evidence for a moderator to weigh. It does not prove that the accounts have
one owner, because households, schools, offices and carrier NAT put unrelated people on one address. The registration
policies therefore restrict only the moves that a sockpuppet operator profits from. Transfers between linked accounts
are not refused. They are reported with a structured event and metrics for review.

A pseudonymous registration from a network with review signals (a recent flagged or deleted account, or too many recent
registrations) needs a higher reCAPTCHA score instead of being refused, so a person on a shared network can still
register without a moderator. When reCAPTCHA is not configured, those registrations are allowed and reported.

The links store the address pseudonym (``CounterMeasures.ip_subject_key``). They do not store the address. Without a
usable deployment secret, the pseudonym is not computed, links are not recorded, and every policy here allows the
request.

Public members:

- [`record_account_link`][horde.account_links.record_account_link]: record an account on a network.
- [`evaluate_registration`][horde.account_links.evaluate_registration]: the registration policies.
- [`report_registration`][horde.account_links.report_registration]: the review mark and event for a registration with
  review signals.
- [`assess_linked_transfer`][horde.account_links.assess_linked_transfer] and
  [`report_linked_transfer`][horde.account_links.report_linked_transfer]: the advisory transfer check.
- [`report_shared_network_generation`][horde.account_links.report_shared_network_generation]: the advisory check of a
  generation fulfilled from the requester's network.
- [`list_account_links`][horde.account_links.list_account_links] and
  [`list_subject_accounts`][horde.account_links.list_subject_accounts]: the moderator views.
- [`purge_expired_account_links`][horde.account_links.purge_expired_account_links]: the retention job.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import logfire
import redis
from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from horde.classes.base.account_link import IpSubjectAccountLink
from horde.classes.base.user import User, UserRole
from horde.countermeasures import CounterMeasures
from horde.enums import UserRoleTypes
from horde.flask import SQLITE_MODE, db
from horde.horde_redis import horde_redis as hr
from horde.logger import logger
from horde.metrics import (
    account_links_decisions,
    account_links_purged,
    account_links_shared_network_generation_kudos,
    account_links_shared_network_generations,
)
from horde.recaptcha import linked_network_min_score
from horde.suspicions import Suspicions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from horde.recaptcha import RecaptchaVerdict

__all__ = [
    "LINKED_TRANSFER_EVENT",
    "REGISTRATION_EVENT",
    "SHARED_NETWORK_GENERATION_EVENT",
    "AccountLinkLimits",
    "AccountLinkPolicy",
    "AccountLinkWindows",
    "LinkDecisionOutcome",
    "LinkStatus",
    "LinkedAccount",
    "LinkedNetwork",
    "LinkedTransferAssessment",
    "RegistrationDecision",
    "RegistrationSignal",
    "assess_linked_transfer",
    "evaluate_registration",
    "list_account_links",
    "list_subject_accounts",
    "purge_expired_account_links",
    "record_account_link",
    "report_linked_transfer",
    "report_registration",
    "report_shared_network_generation",
]


class AccountLinkWindows:
    """Group the time windows and thresholds of the linked-account policies."""

    RETENTION: timedelta = timedelta(days=30)
    """How long a link is kept after it was last seen, and how far back the policies look."""
    PSEUDONYMOUS_REGISTRATION_WINDOW: timedelta = timedelta(days=1)
    """The window in which pseudonymous registrations from one network are counted."""
    PSEUDONYMOUS_REGISTRATION_LIMIT: int = 3
    """The number of pseudonymous registrations from one network in the window before the next one needs a higher score."""
    GENERATION_RECORD_INTERVAL: timedelta = timedelta(hours=1)
    """How often one account's generations on one network update its link, at most."""


class AccountLinkLimits:
    """Group the size bounds of the moderator listings, the registration event and the retention job.

    These bounds limit what is shown, reported or deleted at once. They do not change a policy decision.
    """

    MAX_LISTED_NETWORKS: int = 50
    """The most networks shown for an account in one moderator listing."""
    MAX_LISTED_ACCOUNTS: int = 50
    """The most accounts shown for a network in one moderator listing."""
    MAX_REPORTED_RESTRICTED_ACCOUNTS: int = 50
    """The most restricted account IDs in one registration event.

    One restricted account is enough for the signal. The bound keeps the event small on a network where many banned
    accounts were seen, such as a carrier NAT.
    """
    PURGE_BATCH_SIZE: int = 5000
    """The most expired links deleted by one retention pass."""


class AccountLinkPolicy(StrEnum):
    """Represents a linked-account policy, as a metric attribute."""

    PSEUDONYMOUS_REGISTRATION = "pseudonymous_registration"
    OAUTH_REGISTRATION = "oauth_registration"
    KUDOS_TRANSFER = "kudos_transfer"
    STYLE_SURCHARGE = "style_surcharge"
    """A style surcharge paid by the requester to the style's author."""


class LinkDecisionOutcome(StrEnum):
    """Represents the outcome of a linked-account policy, as a metric attribute."""

    ALLOWED = "allowed"
    REFUSED = "refused"
    FLAGGED = "flagged"
    """The request was allowed and the account was marked for moderator review."""
    ADVISORY = "advisory"
    """The request had review signals. It was allowed and reported with an event."""
    VERIFIED = "verified"
    """The registration had review signals and met the higher reCAPTCHA score."""


GENERATION_RECORD_KEY_PREFIX: str = "account_link_seen:"
"""Prefix of the Redis key that throttles generation link updates for one account on one network."""


class RegistrationSignal(StrEnum):
    """Represents a reason to look at a registration more closely, as an event attribute."""

    LINKED_TO_RESTRICTED_ACCOUNT = "linked_to_restricted_account"
    """A flagged or deleted account used the network in the retention window."""
    REGISTRATION_LIMIT = "registration_limit"
    """The network reached ``PSEUDONYMOUS_REGISTRATION_LIMIT`` pseudonymous registrations in the registration window."""


REGISTRATION_EVENT: str = "horde.account_links.registration_signals"
"""The name of the structured event for a registration with review signals."""
REGISTRATION_REFUSED_MESSAGE: str = "This registration could not be confirmed from your network."
"""The message for a pseudonymous registration that did not meet the higher reCAPTCHA score."""


@dataclass(frozen=True)
class RegistrationDecision:
    """Represents the outcome of a registration policy."""

    policy: AccountLinkPolicy
    """The policy that made the decision."""
    outcome: LinkDecisionOutcome
    """The outcome of the policy."""
    signals: tuple[RegistrationSignal, ...] = ()
    """The review signals of the network. A decision without signals is not reported."""
    linked_restricted_user_ids: list[int] = field(default_factory=list)
    """The flagged or deleted accounts seen on the network in the window, at most ``MAX_REPORTED_RESTRICTED_ACCOUNTS``."""
    recent_pseudonymous_registrations: int = 0
    """The pseudonymous accounts created in the registration window and seen on the network."""
    recaptcha_score: float | None = None
    """The reCAPTCHA score of the registration, when one was verified."""
    required_score: float | None = None
    """The higher reCAPTCHA score that the registration had to meet, when one applied."""
    subject_key: str | None = None
    """The network pseudonym of the registration."""
    reason: str | None = None
    """A message for the registrant when the registration is refused."""

    @property
    def allowed(self) -> bool:
        """Whether the registration may proceed."""
        return self.outcome is not LinkDecisionOutcome.REFUSED


@dataclass(frozen=True)
class LinkedAccount:
    """Represents one account seen on a network, for the moderator views."""

    user_id: int
    username: str
    first_seen: datetime
    last_seen: datetime
    flagged: bool
    deleted: bool
    trusted: bool


@dataclass(frozen=True)
class LinkedNetwork:
    """Represents a network where an account was seen, with the other accounts seen there, for the moderator views."""

    subject_key: str
    first_seen: datetime
    last_seen: datetime
    other_accounts: list[LinkedAccount]


def _window_start(now: datetime | None = None) -> datetime:
    return (now or datetime.utcnow()) - AccountLinkWindows.RETENTION


def _subject_key_for(ipaddr: str | None) -> str | None:
    return CounterMeasures.ip_subject_key(CounterMeasures.ip_subject(ipaddr))


def _is_recordable(user: User) -> bool:
    """Return whether an account's networks are recorded.

    The anonymous account is shared by everyone. A service account submits requests for many people from the service's
    servers, so the network does not show who owns the account.
    """
    return not user.is_anon() and not user.service


def _claim_generation_record(user_id: int, subject_key: str) -> bool:
    """Return whether this generation should update the link, at most once per interval per account and network.

    The claim fails closed. Without the throttle every generation would run an upsert, so a missing or failing Redis
    skips the record. A link only needs to be seen once in the retention window, so a skipped record loses little.
    """
    redis_client = hr.horde_r
    if redis_client is None:
        return False
    claim_key = f"{GENERATION_RECORD_KEY_PREFIX}{user_id}:{subject_key}"
    try:
        return bool(redis_client.set(claim_key, 1, ex=AccountLinkWindows.GENERATION_RECORD_INTERVAL, nx=True))
    except redis.RedisError as error:
        logger.warning(f"Account link throttle failed ({type(error).__name__})")
        return False


def record_account_link(user: User, ipaddr: str | None, *, throttle: bool = False, commit: bool = False) -> bool:
    """Record that the account was seen on the request's network.

    The row is inserted, or its ``last_seen`` is updated, in one statement, so concurrent requests do not race. The
    statement runs in a savepoint. A failed write is logged and rolled back to the savepoint, so a recording problem
    does not fail the request or the caller's transaction. Only database errors are handled. Any other error is raised.

    Args:
        user: The account.
        ipaddr: The request's origin.
        throttle: Update the link at most once per ``GENERATION_RECORD_INTERVAL``, and skip it when Redis is missing or
            fails. Generations pass True, because they are frequent.
        commit: Commit the session after the record. Otherwise the record is committed with the caller's next commit.

    Returns:
        True when a row was written, and committed when ``commit`` is True. False when the account is not recorded,
        the origin does not have a pseudonym, the throttle skipped the record, or the write failed.
    """
    if not _is_recordable(user):
        return False
    subject_key = _subject_key_for(ipaddr)
    if subject_key is None:
        return False
    if throttle and not _claim_generation_record(user.id, subject_key):
        return False
    now = datetime.utcnow()
    table = IpSubjectAccountLink.__table__
    insert = sqlite_insert(table) if SQLITE_MODE else postgresql_insert(table)
    statement = insert.values(subject_key=subject_key, user_id=user.id, first_seen=now, last_seen=now).on_conflict_do_update(
        index_elements=[table.c.subject_key, table.c.user_id],
        set_={"last_seen": now},
    )
    try:
        with db.session.begin_nested():
            db.session.execute(statement)
    except SQLAlchemyError as error:
        logger.warning(f"Account link record failed ({type(error).__name__})")
        return False
    if not commit:
        return True
    try:
        db.session.commit()
    except SQLAlchemyError as error:
        logger.warning(f"Account link commit failed ({type(error).__name__})")
        db.session.rollback()
        return False
    return True


def _restricted_role_filter() -> object:
    return and_(
        UserRole.user_role.in_([UserRoleTypes.FLAGGED, UserRoleTypes.DELETED]),
        UserRole.value.is_(True),
    )


def _find_restricted_accounts_on_subject(subject_key: str, *, exclude_user_id: int | None = None) -> list[int]:
    query = (
        db.session.query(IpSubjectAccountLink.user_id)
        .join(UserRole, UserRole.user_id == IpSubjectAccountLink.user_id)
        .filter(
            IpSubjectAccountLink.subject_key == subject_key,
            IpSubjectAccountLink.last_seen >= _window_start(),
            _restricted_role_filter(),
        )
    )
    if exclude_user_id is not None:
        query = query.filter(IpSubjectAccountLink.user_id != exclude_user_id)
    rows = query.distinct().limit(AccountLinkLimits.MAX_REPORTED_RESTRICTED_ACCOUNTS).all()
    return sorted(row.user_id for row in rows)


def _count_recent_pseudonymous_registrations(subject_key: str) -> int:
    """Return how many pseudonymous accounts were created in the registration window and seen on the network.

    An account seen on the network for any request counts, not only one registered from it. An account registered
    elsewhere and used on the network within the window also counts.

    Example:
        With a one-day window, the network holds links for a pseudonymous account created two hours ago, a pseudonymous
        account created a week ago and an OAuth account created an hour ago. The count is 1.
    """
    since = datetime.utcnow() - AccountLinkWindows.PSEUDONYMOUS_REGISTRATION_WINDOW
    statement = (
        select(func.count())
        .select_from(IpSubjectAccountLink)
        .join(User, User.id == IpSubjectAccountLink.user_id)
        .where(
            # One link row exists per account and network, so counting rows counts accounts.
            IpSubjectAccountLink.subject_key == subject_key,
            # An account created in the window was seen on the network after its creation, so its link is also in the
            # window and ``last_seen`` does not need a condition.
            User.created >= since,
            User.pseudonymous_condition(),
        )
    )
    return db.session.execute(statement).scalar_one()


def evaluate_registration(
    ipaddr: str | None,
    *,
    pseudonymous: bool,
    recaptcha: RecaptchaVerdict | None = None,
) -> RegistrationDecision:
    """Decide whether a new account may register from the request's network.

    A flagged or deleted account on the network in the retention window is the usual way to come back after a ban, so
    it is a review signal for both kinds of account.

    An OAuth account is tied to an outside identity, so it is always allowed. With the signal, the outcome is
    ``flagged``, and [`report_registration`][horde.account_links.report_registration] marks the new account.

    A pseudonymous account costs nothing to make, so ``PSEUDONYMOUS_REGISTRATION_LIMIT`` pseudonymous registrations from
    the network in the registration window is a second signal. Both signals also appear for a school, an office or a
    carrier NAT, so a signal does not refuse a pseudonymous registration by itself.

    - Without signals, the registration is allowed.
    - With signals and a reCAPTCHA verdict, the registration must meet ``linked_network_min_score()``. A registrant who
      does not meet it can still register with an OAuth provider.
    - With signals and without reCAPTCHA, the registration is allowed and reported.

    Args:
        ipaddr: The request's origin.
        pseudonymous: Whether the account is created without an OAuth provider.
        recaptcha: The verdict of the registration's reCAPTCHA token, which passed the minimum score. None when
            reCAPTCHA is not configured, and for an OAuth registration.

    Returns:
        The decision. Pass it to [`report_registration`][horde.account_links.report_registration].
    """
    policy = AccountLinkPolicy.PSEUDONYMOUS_REGISTRATION if pseudonymous else AccountLinkPolicy.OAUTH_REGISTRATION
    subject_key = _subject_key_for(ipaddr)
    if subject_key is None:
        return RegistrationDecision(policy=policy, outcome=LinkDecisionOutcome.ALLOWED)
    restricted_user_ids = _find_restricted_accounts_on_subject(subject_key)
    signals: list[RegistrationSignal] = []
    if restricted_user_ids:
        signals.append(RegistrationSignal.LINKED_TO_RESTRICTED_ACCOUNT)
    recent_registrations = 0
    if pseudonymous:
        recent_registrations = _count_recent_pseudonymous_registrations(subject_key)
        if recent_registrations >= AccountLinkWindows.PSEUDONYMOUS_REGISTRATION_LIMIT:
            signals.append(RegistrationSignal.REGISTRATION_LIMIT)
    required_score: float | None = None
    if not signals:
        outcome = LinkDecisionOutcome.ALLOWED
    elif not pseudonymous:
        outcome = LinkDecisionOutcome.FLAGGED
    elif recaptcha is None:
        outcome = LinkDecisionOutcome.ADVISORY
    else:
        required_score = linked_network_min_score()
        outcome = LinkDecisionOutcome.VERIFIED if recaptcha.meets(required_score) else LinkDecisionOutcome.REFUSED
    account_links_decisions.add(1, {"horde.policy": policy.value, "horde.outcome": outcome.value})
    return RegistrationDecision(
        policy=policy,
        outcome=outcome,
        signals=tuple(signals),
        linked_restricted_user_ids=restricted_user_ids,
        recent_pseudonymous_registrations=recent_registrations,
        recaptcha_score=recaptcha.score if recaptcha is not None else None,
        required_score=required_score,
        subject_key=subject_key,
        reason=REGISTRATION_REFUSED_MESSAGE if outcome is LinkDecisionOutcome.REFUSED else None,
    )


def report_registration(decision: RegistrationDecision, user: User | None = None) -> None:
    """Report a registration with review signals, so that a moderator can look at it when they choose to.

    Moderators are not notified. The registration emits the ``REGISTRATION_EVENT`` structured event with account IDs,
    the signals, the outcome, the reCAPTCHA score and a prefix of the network pseudonym. The event does not include the
    address. A new account linked to a flagged or deleted account also gets the ``LINKED_TO_RESTRICTED_ACCOUNT``
    suspicion, which is one point toward the suspicion threshold and is shown in the account's details.

    Args:
        decision: The decision of a registration policy.
        user: The new account. None when the registration was refused.
    """
    if not decision.signals:
        return
    if user is not None and RegistrationSignal.LINKED_TO_RESTRICTED_ACCOUNT in decision.signals:
        user.report_suspicion(1, Suspicions.LINKED_TO_RESTRICTED_ACCOUNT)
    logfire.info(
        REGISTRATION_EVENT,
        policy=decision.policy.value,
        outcome=decision.outcome.value,
        signals=[signal.value for signal in decision.signals],
        user_id=user.id if user is not None else None,
        linked_restricted_user_ids=list(decision.linked_restricted_user_ids),
        recent_pseudonymous_registrations=decision.recent_pseudonymous_registrations,
        recaptcha_score=decision.recaptcha_score,
        required_score=decision.required_score,
        subject_key_prefix=decision.subject_key[:12] if decision.subject_key else None,
    )


def _count_shared_networks(first_user_id: int, second_user_id: int) -> int:
    """Return how many networks both accounts were seen on within the retention window.

    Example:
        The first account was seen on networks A and B, and the second on B and C, all within the window. The count
        is 1. If the second account's link to B is older than the window, the count is 0.
    """
    first_link = aliased(IpSubjectAccountLink)
    second_link = aliased(IpSubjectAccountLink)
    window_start = _window_start()
    statement = (
        select(func.count())
        .select_from(first_link)
        # Pair each of the first account's links with every link on the same network.
        .join(second_link, second_link.subject_key == first_link.subject_key)
        .where(
            # Keep the pairs where the other link belongs to the second account. One link row exists per account and
            # network, so each shared network yields one pair.
            first_link.user_id == first_user_id,
            second_link.user_id == second_user_id,
            # Both accounts must have been seen on the network within the window.
            first_link.last_seen >= window_start,
            second_link.last_seen >= window_start,
        )
    )
    return db.session.execute(statement).scalar_one()


class LinkStatus(StrEnum):
    """Represents the link status of a transfer, as a metric attribute."""

    UNLINKED = "unlinked"
    LINKED = "linked"
    EXEMPT = "exempt"
    """The sender is trusted or the receiver is an education account, so the link was not checked.

    Trusted accounts have a track record, and classrooms share one network and pool kudos on purpose.
    """


@dataclass(frozen=True, slots=True)
class LinkedTransferAssessment:
    """Represents what the linked-account check found for a transfer between two accounts.

    The check is advisory. A transfer is not refused because of a link, and a linked transfer is reported with a
    structured event and the ``horde.account_links.decisions`` metric for review.
    """

    status: LinkStatus
    """Whether the accounts were linked, not linked, or exempt from the check."""
    shared_networks: int = 0
    """The number of networks shared by the two accounts in the retention window."""


LINKED_TRANSFER_EVENT: str = "horde.account_links.linked_transfer"
"""The name of the structured event emitted for a transfer between linked accounts."""


def assess_linked_transfer(source_user: User, dest_user: User) -> LinkedTransferAssessment:
    """Return whether two accounts in a transfer are linked, without refusing or reporting anything.

    Moving kudos from many fresh accounts to one is how farmed kudos are cashed out. The assessment is evidence for
    review. It does not prove that the accounts have one owner.

    Args:
        source_user: The sender.
        dest_user: The receiver.

    Returns:
        The assessment. A trusted sender or an education receiver is exempt without a database query.

    Performance:
        One query for the shared networks.
    """
    if source_user.id == dest_user.id or source_user.is_anon() or dest_user.is_anon():
        return LinkedTransferAssessment(status=LinkStatus.UNLINKED)
    if source_user.trusted or dest_user.education:
        return LinkedTransferAssessment(status=LinkStatus.EXEMPT)
    shared_networks = _count_shared_networks(source_user.id, dest_user.id)
    if shared_networks == 0:
        return LinkedTransferAssessment(status=LinkStatus.UNLINKED)
    return LinkedTransferAssessment(status=LinkStatus.LINKED, shared_networks=shared_networks)


def report_linked_transfer(
    assessment: LinkedTransferAssessment,
    source_user: User,
    dest_user: User,
    *,
    policy: AccountLinkPolicy,
    amount: float | None = None,
) -> None:
    """Report a completed transfer between linked accounts, for review. Other transfers are not reported.

    A linked transfer emits the ``LINKED_TRANSFER_EVENT`` structured event and counts as ``advisory`` in
    ``horde.account_links.decisions``. The event holds account IDs and the number of shared networks. It does not hold
    an address or a network pseudonym.

    Args:
        assessment: The assessment made for the transfer.
        source_user: The sender.
        dest_user: The receiver.
        policy: The kind of transfer.
        amount: The kudos moved, or None for a transfer of something else.
    """
    if assessment.status is not LinkStatus.LINKED:
        return
    account_links_decisions.add(1, {"horde.policy": policy.value, "horde.outcome": LinkDecisionOutcome.ADVISORY.value})
    logfire.info(
        LINKED_TRANSFER_EVENT,
        policy=policy.value,
        source_user_id=source_user.id,
        dest_user_id=dest_user.id,
        amount=amount,
        shared_networks=assessment.shared_networks,
    )


SHARED_NETWORK_GENERATION_EVENT: str = "horde.account_links.shared_network_generation"
"""The name of the structured event emitted for a generation where the requester and the worker shared a network."""


def report_shared_network_generation(
    *,
    requester: User,
    requester_ipaddr: str | None,
    worker_owner_id: int,
    worker_id: str,
    worker_ipaddr: str | None,
    kudos: float,
) -> None:
    """Report a completed generation where the requester and the worker were on the same network, for review.

    The request's address is compared with the worker's latest address, by IP subject. A generation for the worker's
    owner is not reported. The generation and its kudos are not affected. The event holds the account and worker IDs
    and the kudos. It does not hold an address or a network pseudonym.

    Args:
        requester: The account that made the request.
        requester_ipaddr: The address that the request came from.
        worker_owner_id: The worker owner's account ID.
        worker_id: The worker's ID.
        worker_ipaddr: The worker's latest address.
        kudos: The kudos of the generation.

    Performance:
        Two HMAC computations. It does not query the database.
    """
    if requester.id == worker_owner_id or not requester_ipaddr or not worker_ipaddr:
        return
    requester_subject = _subject_key_for(requester_ipaddr)
    if requester_subject is None or requester_subject != _subject_key_for(worker_ipaddr):
        return
    requester_kind = "anonymous" if requester.is_anon() else "registered"
    attributes = {"horde.requester": requester_kind}
    account_links_shared_network_generations.add(1, attributes)
    account_links_shared_network_generation_kudos.add(float(kudos), attributes)
    logfire.info(
        SHARED_NETWORK_GENERATION_EVENT,
        requester_id=requester.id,
        requester_kind=requester_kind,
        worker_owner_id=worker_owner_id,
        worker_id=worker_id,
        kudos=float(kudos),
    )


def _role_map(user_ids: Sequence[int]) -> dict[int, set[UserRoleTypes]]:
    roles_by_user: dict[int, set[UserRoleTypes]] = {user_id: set() for user_id in user_ids}
    if not user_ids:
        return roles_by_user
    role_rows = (
        db.session.query(UserRole.user_id, UserRole.user_role).filter(UserRole.user_id.in_(list(user_ids)), UserRole.value.is_(True)).all()
    )
    for role_row in role_rows:
        roles_by_user[role_row.user_id].add(role_row.user_role)
    return roles_by_user


def _accounts_on_subjects(subject_keys: Sequence[str], *, exclude_user_id: int | None = None) -> dict[str, list[LinkedAccount]]:
    """Return the accounts seen on each network within the retention window, most recently seen first.

    Example:
        Network A holds links for accounts 7 (seen today), 9 (seen yesterday) and 12 (seen 40 days ago), and network B
        holds a link for account 9. With ``exclude_user_id=9``, the result is ``{"A": [account 7]}``. Account 12 is
        outside the window. Network B is not a key, because it has no other account.

    Args:
        subject_keys: The network pseudonyms.
        exclude_user_id: An account to leave out, such as the account under review.

    Returns:
        At most ``MAX_LISTED_ACCOUNTS`` accounts for each network. A network without accounts is not a key.

    Performance:
        Two queries: the ranked links with usernames, and the roles.
    """
    if not subject_keys:
        return {}
    conditions = [
        IpSubjectAccountLink.subject_key.in_(list(subject_keys)),
        IpSubjectAccountLink.last_seen >= _window_start(),
    ]
    if exclude_user_id is not None:
        conditions.append(IpSubjectAccountLink.user_id != exclude_user_id)
    ranked_links = (
        select(
            IpSubjectAccountLink.subject_key,
            IpSubjectAccountLink.user_id,
            IpSubjectAccountLink.first_seen,
            IpSubjectAccountLink.last_seen,
            User.username,
            # Number each network's accounts from the most recently seen, so the outer query keeps the first
            # MAX_LISTED_ACCOUNTS of each network. A carrier NAT can hold thousands of accounts.
            func.row_number()
            .over(partition_by=IpSubjectAccountLink.subject_key, order_by=IpSubjectAccountLink.last_seen.desc())
            .label("rank"),
        )
        .join(User, User.id == IpSubjectAccountLink.user_id)
        .where(*conditions)
        .subquery()
    )
    rows = db.session.execute(
        select(ranked_links)
        .where(ranked_links.c.rank <= AccountLinkLimits.MAX_LISTED_ACCOUNTS)
        .order_by(ranked_links.c.subject_key, ranked_links.c.rank),
    ).all()
    roles_by_user = _role_map(sorted({row.user_id for row in rows}))
    accounts_by_subject: dict[str, list[LinkedAccount]] = defaultdict(list)
    for row in rows:
        roles = roles_by_user[row.user_id]
        accounts_by_subject[row.subject_key].append(
            LinkedAccount(
                user_id=row.user_id,
                username=f"{row.username}#{row.user_id}",
                first_seen=row.first_seen,
                last_seen=row.last_seen,
                flagged=UserRoleTypes.FLAGGED in roles,
                deleted=UserRoleTypes.DELETED in roles,
                trusted=UserRoleTypes.TRUSTED in roles,
            ),
        )
    return dict(accounts_by_subject)


def list_subject_accounts(subject_key: str, *, exclude_user_id: int | None = None) -> list[LinkedAccount]:
    """Return the accounts seen on one network within the retention window, most recently seen first.

    Args:
        subject_key: The network pseudonym.
        exclude_user_id: An account to leave out, such as the account under review.

    Returns:
        At most ``MAX_LISTED_ACCOUNTS`` accounts.

    Performance:
        Two queries: the links with usernames, and the roles.
    """
    return _accounts_on_subjects([subject_key], exclude_user_id=exclude_user_id).get(subject_key, [])


def list_account_links(user_id: int) -> list[LinkedNetwork]:
    """Return the networks where an account was seen within the retention window, with the other accounts seen on each.

    Networks with other accounts come first, then the most recently seen.

    Args:
        user_id: The account.

    Returns:
        At most ``MAX_LISTED_NETWORKS`` networks, each with at most ``MAX_LISTED_ACCOUNTS`` other accounts.

    Performance:
        Three queries: the account's links, the other accounts' links with usernames, and their roles.
    """
    own_links = (
        db.session.query(IpSubjectAccountLink.subject_key, IpSubjectAccountLink.first_seen, IpSubjectAccountLink.last_seen)
        .filter(IpSubjectAccountLink.user_id == user_id, IpSubjectAccountLink.last_seen >= _window_start())
        .order_by(IpSubjectAccountLink.last_seen.desc())
        .limit(AccountLinkLimits.MAX_LISTED_NETWORKS)
        .all()
    )
    others_by_subject = _accounts_on_subjects([link.subject_key for link in own_links], exclude_user_id=user_id)
    linked_networks = [
        LinkedNetwork(
            subject_key=link.subject_key,
            first_seen=link.first_seen,
            last_seen=link.last_seen,
            other_accounts=others_by_subject.get(link.subject_key, []),
        )
        for link in own_links
    ]
    # The sort is stable, so the most recently seen network stays first within each group.
    linked_networks.sort(key=lambda network: len(network.other_accounts) == 0)
    return linked_networks


def purge_expired_account_links() -> int:
    """Delete one batch of links last seen before the retention window. The caller commits.

    Returns:
        The deleted rows.
    """
    expired_ids = (
        select(IpSubjectAccountLink.id)
        .where(IpSubjectAccountLink.last_seen < _window_start())
        .limit(AccountLinkLimits.PURGE_BATCH_SIZE)
        .scalar_subquery()
    )
    result = db.session.execute(delete(IpSubjectAccountLink).where(IpSubjectAccountLink.id.in_(expired_ids)))
    deleted_rows = result.rowcount or 0
    if deleted_rows:
        account_links_purged.add(deleted_rows)
    return deleted_rows
