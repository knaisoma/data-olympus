"""Who may see what in Agora.

Rules (design 6.3):

- universal and stack scopes are open to every participant unless an admin
  has blocked them, or an owned agent's owner, instance-wide;
- project and component scopes are for effective members of the project;
- an owned agent's access depends only on its owner: it is a member exactly
  when its owner is, and it loses access the moment its owner is removed or
  blocked, in the project or instance-wide. Its own membership grants nothing,
  though blocking the agent itself still cuts it;
- a direct conversation is visible to its two ends, their owners and admins;
- with auth off (one trusted local user) membership is not checked.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import INSTANCE, Membership, Scope
from data_olympus.agora.scopes import ScopeRef, parse_scope

if TYPE_CHECKING:
    from data_olympus.agora.context import AgoraContext
    from data_olympus.agora.models import Participant, Thread


def project_of(scope_id: str) -> str | None:
    ref = parse_scope(scope_id)
    return f"project:{ref.project}" if ref.project else None


class Access:
    def __init__(self, ctx: AgoraContext) -> None:
        self.ctx = ctx

    # --- scopes ---------------------------------------------------------------
    def exists(self, scope_id: str) -> bool:
        return (scope_id == "universal" or self.ctx.store.get_scope(scope_id) is not None
                or scope_id in self.ctx.categories())

    def _ensure(self, ref: ScopeRef) -> Scope:
        store = self.ctx.store
        scope = store.get_scope(ref.id)
        if scope is not None:
            return scope
        cats = self.ctx.categories()
        if ref.id != "universal" and ref.id not in cats:
            raise AgoraError("not_found", f"no scope {ref.id!r}")
        now = self.ctx.clock()
        if ref.project and ref.component:
            pid = f"project:{ref.project}"
            store.insert_scope(Scope(pid, "T3", None, None, "", pid in cats, now))
        store.insert_scope(Scope(ref.id, ref.tier, None, None, "", True, now))
        stored = store.get_scope(ref.id)
        assert stored is not None
        return stored

    def resolve_scope(self, actor: Participant, raw: str, *, claim: bool) -> Scope:
        """The scope ``raw`` names, seeding a KB scope on first sight.

        With ``claim``, auth on, a person actor and a project nobody owns yet,
        the actor becomes its owner (design 6.3: the first person to open a
        KB-seeded project claims it). A blocked person never claims.
        """
        ref = parse_scope(raw)
        scope = self._ensure(ref)
        if (claim and self.ctx.settings.auth_on and actor.kind == "person"
                and ref.project is not None):
            pid = f"project:{ref.project}"
            project = scope if ref.id == pid else self._ensure(parse_scope(pid))
            if (project.owner is None and not self.instance_blocked(actor.name)
                    and not self._blocked(pid, actor.name)):
                now = self.ctx.clock()
                won = self.ctx.store.claim_project(
                    pid, actor.name,
                    Membership(pid, actor.name, "owner", "active", None, now),
                )
                if won:
                    self.ctx.record("scope.claim", actor, scope=pid)
                if ref.id == pid:
                    refreshed = self.ctx.store.get_scope(pid)
                    assert refreshed is not None
                    scope = refreshed
        return scope

    # --- membership -------------------------------------------------------
    def _membership(self, project: str, name: str) -> Membership | None:
        return self.ctx.store.get_membership(project, name)

    def _blocked(self, project: str, name: str) -> bool:
        m = self._membership(project, name)
        return m is not None and m.state == "blocked"

    def instance_blocked(self, name: str) -> bool:
        return self._blocked(INSTANCE, name)

    def is_effective_member(self, project: str, name: str) -> bool:
        rec = self.ctx.known(name)
        if rec is None:
            return False
        if self.instance_blocked(name) or self._blocked(project, name):
            return False
        if rec.kind == "agent" and rec.owner:
            owner = rec.owner
            if self.instance_blocked(owner) or self._blocked(project, owner):
                return False
            owner_rec = self.ctx.known(owner)
            if owner_rec is None or owner_rec.kind != "person":
                return False
            m = self._membership(project, owner)
            return m is not None and m.state == "active"
        m = self._membership(project, name)
        return m is not None and m.state == "active"

    def is_project_owner(self, actor: Participant, project: str) -> bool:
        """An admin, or a person holding an active owner membership."""
        if actor.admin:
            return True
        if actor.kind != "person" or self.instance_blocked(actor.name):
            return False
        m = self._membership(project, actor.name)
        return m is not None and m.state == "active" and m.role == "owner"

    # --- visibility ---------------------------------------------------------
    def can_read_scope(self, actor: Participant, scope: Scope) -> bool:
        if not self.ctx.settings.auth_on or actor.admin:
            return True
        if scope.tier in ("T1", "T2"):
            if self.instance_blocked(actor.name):
                return False
            return not (actor.owner and self.instance_blocked(actor.owner))
        project = project_of(scope.id)
        return project is not None and self.is_effective_member(project, actor.name)

    def scope_record(self, scope_id: str) -> Scope:
        scope = self.ctx.store.get_scope(scope_id)
        if scope is None:
            scope = self._ensure(parse_scope(scope_id))
        return scope

    def can_read_thread(self, actor: Participant, thread: Thread) -> bool:
        try:
            scope = self.scope_record(thread.scope)
        except AgoraError:
            return False
        if not self.can_read_scope(actor, scope):
            return False
        if thread.audience_type != "participant" or actor.admin:
            return True
        ends = {thread.originator, thread.audience_value}
        allowed = set(ends)
        for end in ends:
            rec = self.ctx.known(end)
            if rec is not None and rec.owner:
                allowed.add(rec.owner)
        return actor.name in allowed

    @staticmethod
    def require(cond: bool, message: str) -> None:
        if not cond:
            raise AgoraError("forbidden", message)
