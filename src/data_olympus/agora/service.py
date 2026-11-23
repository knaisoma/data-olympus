"""Agora core service: profiles, scopes, membership, contacts, subscriptions.

Every state change is recorded in Agora's own audit log. Nothing here writes
to the knowledge base.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import (
    INSTANCE,
    Contact,
    Membership,
    Profile,
    Scope,
    Subscription,
)
from data_olympus.agora.safety import validate_text
from data_olympus.agora.scopes import parse_scope

if TYPE_CHECKING:
    from collections.abc import Sequence

    from data_olympus.agora.access import Access
    from data_olympus.agora.context import AgoraContext
    from data_olympus.agora.models import Participant

# Free tags on profiles and thread topics.
TAG_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
MAX_SKILLS = 20
DESCRIPTION_MAX_CHARS = 2000
SHORT_TEXT_MAX_CHARS = 200
NOTE_MAX_CHARS = 500
REMOTE_URL_MAX_CHARS = 500
AUTONOMY_LEVELS = ("notify", "reply", "act")
THREAD_KINDS = ("discussion", "question", "decision", "handover")

MembershipAction = Literal[
    "invite", "accept", "decline", "remove", "block", "unblock", "set_role"]
ContactAction = Literal["request", "accept", "revoke"]


def validate_tags(values: Sequence[str], *, field: str, limit: int) -> tuple[str, ...]:
    if isinstance(values, str):
        raise AgoraError("invalid", f"{field} must be a list of tags")
    out: list[str] = []
    for raw in values:
        tag = str(raw).strip().lower()
        if not TAG_PATTERN.fullmatch(tag):
            raise AgoraError("invalid", f"{field} tags must match "
                             f"{TAG_PATTERN.pattern}; got {tag[:40]!r}")
        if tag not in out:
            out.append(tag)
    if len(out) > limit:
        raise AgoraError("invalid", f"at most {limit} {field} allowed")
    return tuple(out)


def short_text(text: str, *, field: str, max_chars: int, required: bool = False) -> str:
    value = validate_text(text, field=field, max_bytes=max_chars * 4, required=required)
    if len(value) > max_chars:
        raise AgoraError("too_large", f"{field} is longer than {max_chars} characters")
    return value


class AgoraService:
    def __init__(self, ctx: AgoraContext, access: Access, highlights: Any = None) -> None:
        self.ctx = ctx
        self.access = access
        self.highlights = highlights

    # --- participants and profiles ----------------------------------------
    def touch(self, actor: Participant) -> None:
        self.ctx.store.upsert_participant(actor, self.ctx.clock())

    def _known_or_404(self, name: str) -> Any:
        rec = self.ctx.known(name)
        if rec is None:
            raise AgoraError("not_found", f"no participant {name[:64]!r}")
        return rec

    def _autonomy_visible_to(self, actor: Participant, name: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for sub in self.ctx.store.list_subscriptions(name):
            try:
                scope = self.access.scope_record(sub.scope)
            except AgoraError:
                continue
            if self.access.can_read_scope(actor, scope):
                out[sub.scope] = sub.autonomy
        return out

    def profile_get(self, actor: Participant, name: str) -> dict[str, Any]:
        rec = self._known_or_404(name)
        profile = self.ctx.store.get_profile(name) or Profile(name)
        return profile.to_agent_card(rec, self._autonomy_visible_to(actor, name))

    def profile_set(
        self, actor: Participant, *, target: str | None = None,
        description: str | None = None, skills: Sequence[str] | None = None,
        tool: str | None = None, availability: str | None = None,
    ) -> Profile:
        name = target or actor.name
        rec = self._known_or_404(name)
        self.access.require(name == actor.name or rec.owner == actor.name,
                            "only the participant or its owner may edit a profile")
        current = self.ctx.store.get_profile(name) or Profile(name)
        changes: dict[str, Any] = {}
        if description is not None:
            changes["description"] = short_text(
                description, field="description", max_chars=DESCRIPTION_MAX_CHARS)
        if skills is not None:
            changes["skills"] = validate_tags(skills, field="skills", limit=MAX_SKILLS)
        if tool is not None:
            changes["tool"] = short_text(tool, field="tool", max_chars=SHORT_TEXT_MAX_CHARS)
        if availability is not None:
            changes["availability"] = short_text(
                availability, field="availability", max_chars=SHORT_TEXT_MAX_CHARS)
        updated = replace(current, **changes, updated_at=self.ctx.clock())
        self.ctx.store.put_profile(updated)
        self.ctx.record("profile.set", actor, target=name, fields=sorted(changes))
        return updated

    # --- scopes -----------------------------------------------------------
    def _candidate_scopes(self) -> list[Scope]:
        stored = {s.id: s for s in self.ctx.store.list_scopes()}
        now = self.ctx.clock()
        if "universal" not in stored:
            stored["universal"] = Scope("universal", "T1", None, None, "", True, now)
        for cat in self.ctx.categories():
            if cat in stored:
                continue
            try:
                ref = parse_scope(cat)
            except AgoraError:
                continue
            stored[cat] = Scope(cat, ref.tier, None, None, "", True, now)
        return [stored[k] for k in sorted(stored)]

    def scopes(self, actor: Participant) -> list[Scope]:
        return [s for s in self._candidate_scopes() if self.access.can_read_scope(actor, s)]

    def scope_create(self, actor: Participant, raw: str, *, remote_url: str = "") -> Scope:
        ref = parse_scope(raw)
        self.access.require(actor.kind == "person", "only a person may create a scope")
        if ref.tier == "T1":
            raise AgoraError("invalid", "universal always exists")
        if ref.tier == "T2":
            self.access.require(actor.admin, "only an agora_admin may create a stack scope")
        self.access.require(not self.access.instance_blocked(actor.name),
                            "blocked participants cannot create scopes")
        if self.access.exists(ref.id):
            raise AgoraError("conflict", f"scope {ref.id!r} already exists")
        url = short_text(remote_url, field="remote_url", max_chars=REMOTE_URL_MAX_CHARS)
        now = self.ctx.clock()
        store = self.ctx.store
        if ref.tier == "T4":
            pid = f"project:{ref.project}"
            if self.access.exists(pid):
                project = self.access.resolve_scope(actor, pid, claim=True)
                self.access.require(self.access.is_project_owner(actor, project.id),
                                    "only the project owner may add a component")
            else:
                store.insert_scope(Scope(pid, "T3", actor.name, actor.name, "", False, now))
                store.put_membership(Membership(pid, actor.name, "owner", "active", None, now))
                self.ctx.record("scope.create", actor, scope=pid)
            scope = Scope(ref.id, "T4", None, actor.name, url, False, now)
            if not store.insert_scope(scope):
                raise AgoraError("conflict", f"scope {ref.id!r} already exists")
        elif ref.tier == "T3":
            scope = Scope(ref.id, "T3", actor.name, actor.name, url, False, now)
            if not store.insert_scope(scope):
                raise AgoraError("conflict", f"scope {ref.id!r} already exists")
            store.put_membership(Membership(ref.id, actor.name, "owner", "active", None, now))
        else:
            scope = Scope(ref.id, "T2", None, actor.name, url, False, now)
            if not store.insert_scope(scope):
                raise AgoraError("conflict", f"scope {ref.id!r} already exists")
        self.ctx.record("scope.create", actor, scope=ref.id)
        return scope

    def directory(self, actor: Participant, raw_scope: str) -> list[dict[str, Any]]:
        scope = self.access.resolve_scope(actor, raw_scope, claim=False)
        self.access.require(self.access.can_read_scope(actor, scope),
                            "you cannot read this scope")
        out = []
        for name in sorted(self.ctx.all_names()):
            other = self.ctx.participant_for(name)
            if other is None or not self.access.can_read_scope(other, scope):
                continue
            out.append({
                "name": other.name, "kind": other.kind, "owner": other.owner,
                "card": self.profile_get(actor, other.name),
            })
        return out

    # --- membership -------------------------------------------------------
    def _owner_count(self, project: str, *, excluding: str | None = None) -> int:
        return sum(
            1 for m in self.ctx.store.list_memberships(project=project)
            if m.role == "owner" and m.state == "active" and m.participant != excluding
        )

    def _emit_membership(self, actor: Participant, m: Membership, verb: str) -> None:
        if self.highlights is not None:
            self.highlights.commit_membership(actor, m, verb)
        else:
            self.ctx.store.put_membership(m)

    def membership(
        self, actor: Participant, *, action: MembershipAction, project: str,
        participant: str, role: Literal["owner", "member"] = "member",
    ) -> Membership:
        store = self.ctx.store
        now = self.ctx.clock()
        if role not in ("owner", "member"):
            raise AgoraError("invalid", "role must be owner or member")
        if project == INSTANCE:
            if action not in ("block", "unblock"):
                raise AgoraError("invalid", "only block and unblock apply instance-wide")
            self.access.require(actor.admin, "only an agora_admin may block instance-wide")
            self._known_or_404(participant)
            state: Literal["blocked", "removed"] = (
                "blocked" if action == "block" else "removed")
            m = Membership(INSTANCE, participant, "member", state, actor.name, now)
            if action == "block":
                self._emit_membership(actor, m, "blocked")
            else:
                store.put_membership(m)
            self.ctx.record(f"membership.{action}", actor, project=INSTANCE,
                            participant=participant)
            return m

        scope = self.access.resolve_scope(actor, project, claim=True)
        if scope.tier != "T3":
            raise AgoraError("invalid", "membership is managed on project scopes")
        pid = scope.id
        rec = self._known_or_404(participant)
        current = store.get_membership(pid, participant)
        is_owner = self.access.is_project_owner(actor, pid)
        acting_for_target = participant == actor.name or rec.owner == actor.name
        result: Membership
        verb: str | None = None

        if action == "invite":
            self.access.require(is_owner, "only a project owner may invite")
            if rec.kind == "agent" and rec.owner:
                raise AgoraError("invalid", "an owned agent inherits its owner's "
                                 "membership; invite the owner instead")
            if role == "owner" and rec.kind != "person":
                raise AgoraError("invalid", "only persons can be owners")
            if current is not None and current.state == "blocked":
                raise AgoraError("conflict", "the participant is blocked; unblock first")
            if current is not None and current.state in ("active", "invited"):
                raise AgoraError("conflict", f"the participant is already {current.state}")
            result = Membership(pid, participant, role, "invited", actor.name, now)
        elif action in ("accept", "decline"):
            self.access.require(acting_for_target,
                                "only the invitee or its owner may answer an invite")
            if current is None or current.state != "invited":
                raise AgoraError("conflict", "there is no pending invite")
            if action == "accept":
                result = replace(current, state="active", updated_at=now)
                verb = "joined"
            else:
                result = replace(current, state="removed", updated_at=now)
        elif action == "remove":
            self.access.require(is_owner or participant == actor.name,
                                "only a project owner may remove others")
            if current is None or current.state not in ("active", "invited"):
                raise AgoraError("conflict", "the participant is not a member")
            if (current.role == "owner" and current.state == "active"
                    and self._owner_count(pid, excluding=participant) == 0):
                raise AgoraError("conflict", "the last owner cannot be removed")
            result = replace(current, state="removed", updated_at=now)
            verb = "removed"
        elif action == "block":
            self.access.require(is_owner, "only a project owner may block")
            if (current is not None and current.role == "owner"
                    and current.state == "active"
                    and self._owner_count(pid, excluding=participant) == 0):
                raise AgoraError("conflict", "the last owner cannot be blocked")
            result = Membership(pid, participant, "member", "blocked", actor.name, now)
            verb = "blocked"
        elif action == "unblock":
            self.access.require(is_owner, "only a project owner may unblock")
            if current is None or current.state != "blocked":
                raise AgoraError("conflict", "the participant is not blocked")
            result = replace(current, state="removed", updated_at=now)
        elif action == "set_role":
            self.access.require(is_owner, "only a project owner may change roles")
            if current is None or current.state != "active":
                raise AgoraError("conflict", "the participant is not an active member")
            if role == "owner" and rec.kind != "person":
                raise AgoraError("invalid", "only persons can be owners")
            if (current.role == "owner" and role == "member"
                    and self._owner_count(pid, excluding=participant) == 0):
                raise AgoraError("conflict", "the last owner cannot be demoted")
            result = replace(current, role=role, updated_at=now)
        else:
            raise AgoraError("invalid", f"unknown membership action {action!r}")

        if verb is not None:
            self._emit_membership(actor, result, verb)
        else:
            store.put_membership(result)
        self._sync_scope_owner(pid)
        self.ctx.record(f"membership.{action}", actor, project=pid,
                        participant=participant, role=result.role, state=result.state)
        return result

    def _sync_scope_owner(self, pid: str) -> None:
        """Keep the scope's recorded owner pointing at an active owner."""
        scope = self.ctx.store.get_scope(pid)
        if scope is None:
            return
        owners = sorted(
            (m for m in self.ctx.store.list_memberships(project=pid)
             if m.role == "owner" and m.state == "active"),
            key=lambda m: m.updated_at,
        )
        names = [m.participant for m in owners]
        if scope.owner in names or (scope.owner is None and not names):
            return
        self.ctx.store.put_scope(replace(scope, owner=names[0] if names else None))

    # --- contacts -----------------------------------------------------------
    def contact(
        self, actor: Participant, *, action: ContactAction, other: str, note: str = "",
        recipient: str | None = None,
    ) -> Contact:
        store = self.ctx.store
        now = self.ctx.clock()
        self._known_or_404(other)
        if action == "request":
            if other == actor.name:
                raise AgoraError("invalid", "you cannot contact yourself")
            text = short_text(note, field="note", max_chars=NOTE_MAX_CHARS)
            existing = store.get_contact(other, actor.name)
            if existing is not None and existing.state == "accepted":
                return existing
            c = Contact(other, actor.name, "requested", text, now)
            store.put_contact(c)
            self.ctx.record("contact.request", actor, recipient=other, **(
                {"note_bytes": len(text.encode())} if text else {}))
            return c
        target = recipient or actor.name
        rec = self._known_or_404(target)
        self.access.require(target == actor.name or rec.owner == actor.name,
                            "only the recipient or its owner may answer a contact")
        existing = store.get_contact(target, other)
        if action == "accept":
            c = Contact(target, other, "accepted", existing.note if existing else "", now)
        elif action == "revoke":
            if existing is None:
                raise AgoraError("not_found", "no such contact")
            c = replace(existing, state="revoked", updated_at=now)
        else:
            raise AgoraError("invalid", f"unknown contact action {action!r}")
        store.put_contact(c)
        self.ctx.record(f"contact.{action}", actor, recipient=target, sender=other)
        return c

    def contact_ok(self, sender: Participant, recipient: str) -> bool:
        if not self.ctx.settings.auth_on:
            return True
        rec = self.ctx.known(recipient)
        if rec is None:
            return False
        if sender.accountable == (rec.owner or rec.name):
            return True
        c = self.ctx.store.get_contact(recipient, sender.name)
        return c is not None and c.state == "accepted"

    # --- subscriptions ------------------------------------------------------
    def subscribe(
        self, actor: Participant, *, scope: str,
        autonomy: Literal["notify", "reply", "act"] = "notify",
        kinds: Sequence[str] = (),
    ) -> Subscription:
        if autonomy not in AUTONOMY_LEVELS:
            raise AgoraError("invalid", "autonomy must be notify, reply or act")
        bad = [k for k in kinds if k not in THREAD_KINDS]
        if bad or isinstance(kinds, str):
            raise AgoraError("invalid", f"kinds must be among {', '.join(THREAD_KINDS)}")
        record = self.access.resolve_scope(actor, scope, claim=False)
        self.access.require(self.access.can_read_scope(actor, record),
                            "you cannot read this scope")
        sub = Subscription(actor.name, record.id, autonomy, tuple(dict.fromkeys(kinds)))
        self.ctx.store.put_subscription(sub)
        self.ctx.record("subscribe", actor, scope=record.id, autonomy=autonomy)
        return sub

    def unsubscribe(self, actor: Participant, *, scope: str) -> None:
        ref = parse_scope(scope)
        self.ctx.store.delete_subscription(actor.name, ref.id)
        self.ctx.record("unsubscribe", actor, scope=ref.id)

    def implicit_scopes(self, name: str) -> set[str]:
        """``universal``, plus every project where ``name`` is an effective
        member and that project's components (all of them with auth off)."""
        out = {"universal"}
        candidates = [s.id for s in self._candidate_scopes() if s.tier in ("T3", "T4")]
        if not self.ctx.settings.auth_on:
            return out | set(candidates)
        members = {
            pid for pid in candidates
            if pid.startswith("project:") and self.access.is_effective_member(pid, name)
        }
        out |= members
        for sid in candidates:
            if sid.startswith("component:"):
                project = "project:" + sid.split(":", 1)[1].split("/", 1)[0]
                if project in members:
                    out.add(sid)
        return out
