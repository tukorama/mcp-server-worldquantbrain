from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field


def register_prompts(server: MCPServer) -> None:
    @server.prompt(title="Design a BRAIN alpha")
    def design_alpha(
        region: Annotated[str, Field(description="Market region, e.g. USA.")] = "USA",
        universe: Annotated[str, Field(description="Universe, e.g. TOP3000.")] = "TOP3000",
        dataset_id: Annotated[str, Field(description="Dataset to explore, optional.")] = "",
        idea: Annotated[str, Field(description="Investment hypothesis, optional.")] = "",
    ) -> str:
        """Research loop: pick data, write an expression, simulate, then improve it."""
        source = f"Use dataset {dataset_id}." if dataset_id else "Pick one with list_datasets."
        hypothesis = f"Hypothesis: {idea}" if idea else "Start from a simple economic hypothesis."
        return f"""Research one alpha for {region} {universe} on WorldQuant BRAIN.

{hypothesis}
{source}

1. Read brain://docs/fastexpr-cheatsheet and pick fields with list_datafields.
2. Write the simplest expression that tests the idea and run simulate_alpha.
3. Read in_sample and failed_checks. Change one thing at a time and say why.
4. Stop after five simulations or once all checks pass.
5. Report the best alpha id, its stats, and get_alpha_self_correlation. Do not submit it."""

    @server.prompt(title="Review a BRAIN alpha")
    def review_alpha(
        alpha_id: Annotated[str, Field(description="Alpha to review.")],
    ) -> str:
        """Assess an alpha's checks, correlation and PnL, and suggest concrete changes."""
        return f"""Review alpha {alpha_id} on WorldQuant BRAIN.

Call get_alpha, check_alpha_submission, get_alpha_self_correlation and get_alpha_pnl. Then say
whether it is ready to submit, which checks block it, whether performance is stable from year to
year, and the single most promising change to try next. Do not submit it."""
