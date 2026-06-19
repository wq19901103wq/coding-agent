"""Simple goal scheduler for Phase 1."""

from agent.supervisor.models import Goal, GoalStatus


class Scheduler:
    """Track goals and their dependencies, returning ready goals."""

    def __init__(self, goals: list[Goal]):
        self.goals: dict[str, Goal] = {g.id: g for g in goals}
        self._done: set[str] = set()

    def ready_goals(self) -> list[Goal]:
        ready: list[Goal] = []
        for goal in self.goals.values():
            if goal.status not in (GoalStatus.PENDING, GoalStatus.IN_PROGRESS):
                continue
            if goal.status == GoalStatus.IN_PROGRESS:
                ready.append(goal)
                continue
            if all(dep in self._done for dep in goal.depends_on):
                ready.append(goal)
        return ready

    def mark_done(self, goal_id: str) -> None:
        self._done.add(goal_id)
        if goal_id in self.goals:
            self.goals[goal_id].status = GoalStatus.DONE

    def mark_in_progress(self, goal_id: str) -> None:
        if goal_id in self.goals:
            self.goals[goal_id].status = GoalStatus.IN_PROGRESS

    def all_done(self) -> bool:
        return all(g.status in (GoalStatus.DONE, GoalStatus.CANCELLED) for g in self.goals.values())
