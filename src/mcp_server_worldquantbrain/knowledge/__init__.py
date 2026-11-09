from pathlib import Path

_ROOT = Path(__file__).parent

TOPICS = ("fastexpr-cheatsheet", "settings-guide", "submission-criteria")

AGENT_GUIDE = """\
Workflow for WorldQuant BRAIN: find data with list_datasets, list_datafields and list_operators, \
then call simulate_alpha. Read in_sample stats and failed_checks in the result and iterate. Before \
proposing submission, call check_alpha_submission and get_alpha_self_correlation.

Simulations spend a finite quota and take minutes. If simulate_alpha returns PENDING, resume with \
get_simulation_status and never start the same simulation again. Tools log in by themselves. If a \
tool reports a biometric check, give the user the URL and call complete_authentication once they \
are done. Submission is permanent: submit only when the user explicitly asks.\
"""


def read(topic: str) -> str:
    return (_ROOT / f"{topic}.md").read_text(encoding="utf-8")
