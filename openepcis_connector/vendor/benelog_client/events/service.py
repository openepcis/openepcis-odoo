# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Delivering events to an EPCIS repository, and finding out what became of them.

Capture is asynchronous, and that is not an implementation detail to be hidden.
``POST /capture`` answers ``202 Accepted`` with a ``Location`` naming a job: the
repository has taken custody of the document and will validate it afterwards.
An event can therefore be *accepted* and later *rejected*, and a connector that
treats the 202 as success reports a delivery that never happened.

So this returns a receipt rather than a boolean, and offers to ask again. A host
adapter can record the receipt against its own row and resolve it on the next
run of its queue — which is exactly what an outbox wants and what a fire-and-
forget call cannot give it.

The repository is a different service from the resolver, on its own host and
with its own permission: capture needs the ``capture`` role, and a token that
publishes master data perfectly well is refused here with a ``403`` that
mentions no role at all. :meth:`Capture.check` exists to turn that into a
sentence somebody can act on, at setup time rather than at the first delivery.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.client import Client
from ..core.errors import BenelogError

#: Where a repository takes documents. Fixed by EPCIS 2.0, not a deployment
#: choice — the standard names the path, and every conformant repository serves
#: it. Only the host in front of it varies.
CAPTURE_PATH = "/capture"


@dataclass(frozen=True)
class CaptureReceipt:
    """What the repository gave back when it accepted a document.

    :param job: identifier of the capture job, taken from ``Location``. Empty
        when the repository answered without one, which is legal and means the
        outcome cannot be asked about later.
    :param event_ids: the identifiers this side minted, kept so a host adapter
        can record what it claimed before it knows whether the claim stuck.
    """

    job: str
    event_ids: tuple[str, ...] = ()

    @property
    def answerable(self) -> bool:
        """Whether :meth:`Capture.outcome` can say anything about this."""
        return bool(self.job)


@dataclass(frozen=True)
class CaptureOutcome:
    """The repository's verdict on a job.

    :param running: still being validated. Neither stored nor refused yet.
    :param success: stored. Only meaningful once ``running`` is false.
    :param errors: what was wrong, in the repository's own words. Never
        paraphrased here — a validation message names a field, and rewriting it
        loses the field.
    """

    running: bool
    success: bool
    errors: tuple[str, ...] = field(default=())

    @property
    def settled(self) -> bool:
        return not self.running


class Capture:
    """Delivery of EPCIS documents into one repository.

    :param client: a :class:`~benelog_client.core.client.Client` pointed at the
        **repository**, not at the resolver. They are two services; giving this
        the resolver's client produces a ``404`` on a path the resolver never
        claimed to serve.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    def submit(self, epcis_document: Mapping[str, Any]) -> CaptureReceipt:
        """Hand a document over.

        :raises BenelogError: if the repository refuses it outright — a
            malformed document, a missing permission, an unreachable host. A
            document that is accepted and *then* found faulty does not raise;
            ask :meth:`outcome`.
        """
        response = self._client.request("POST", CAPTURE_PATH, payload=epcis_document, raw=True)
        return CaptureReceipt(
            job=_job_from(response.headers.get("Location", "")),
            event_ids=_event_ids(epcis_document),
        )

    def outcome(self, receipt: CaptureReceipt | str) -> CaptureOutcome:
        """Ask what became of a job.

        A job the repository no longer knows about is reported as finished and
        successful: capture jobs are retained for a while and then forgotten,
        and "forgotten" only ever follows "stored" — a rejected document keeps
        its job so somebody can read the reason.
        """
        job = receipt if isinstance(receipt, str) else receipt.job
        if not job:
            raise ValueError("this receipt carries no job to ask about")
        try:
            body = self._client.get(f"{CAPTURE_PATH}/{job}")
        except BenelogError as error:
            if error.status == 404:
                return CaptureOutcome(running=False, success=True)
            raise
        return _outcome_from(body or {})

    def check(self) -> str:
        """Whether this deployment will accept events, said in one sentence.

        Returns an empty string when it will. Otherwise a sentence naming what
        is missing — meant to be shown in a settings screen, where it can still
        be fixed, rather than discovered by a queue at three in the morning.
        """
        try:
            self._client.get(CAPTURE_PATH, params={"perPage": 1})
        except BenelogError as error:
            if error.status == 403:
                return (
                    "The repository accepted the credential but refused the request: this "
                    "identity does not hold the 'capture' role. Capture is a separate "
                    "permission from publishing master data."
                )
            if error.status == 401:
                return "The repository did not accept the credential at all."
            if error.status == 404:
                return (
                    "No EPCIS repository answered at this address — the capture endpoint is "
                    "not there. Check that this is the repository's host and not the "
                    "resolver's."
                )
            return f"The repository could not be reached: {error}"
        return ""


def _job_from(location: str) -> str:
    """The job id out of a ``Location``, whether absolute or relative."""
    return location.rstrip("/").rsplit("/", 1)[-1] if location else ""


def _event_ids(epcis_document: Mapping[str, Any]) -> tuple[str, ...]:
    body = epcis_document.get("epcisBody") or {}
    events = body.get("eventList") or []
    return tuple(event["eventID"] for event in events if event.get("eventID"))


def _outcome_from(body: Mapping[str, Any]) -> CaptureOutcome:
    errors = body.get("errors") or []
    return CaptureOutcome(
        running=bool(body.get("running")),
        success=bool(body.get("success")),
        errors=tuple(_error_text(error) for error in errors),
    )


def _error_text(error: Any) -> str:
    if isinstance(error, Mapping):
        for key in ("title", "detail", "message", "type"):
            if error.get(key):
                return str(error[key])
        return str(dict(error))
    return str(error)
