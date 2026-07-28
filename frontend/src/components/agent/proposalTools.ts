/**
 * Which agent tools render as a card instead of a tool row — the single
 * source of truth for that question.
 *
 * This exists because it used to be five independent per-tool allowlists
 * (three folds in `AgentPanel`, its proposals-query invalidation, and
 * `ToolSelector`'s workflow warning). `propose_story_block` shipped in one of
 * them, so an analyst watching a live turn saw bare `propose_story_block` tool
 * rows where the proposal cards belonged. Adding a proposal tool is now a
 * one-line edit here; every path derives from these maps.
 *
 * Lives in its own module rather than in `AgentPanel`: `AgentPanel` imports
 * `ToolSelectorPopover`, so `ToolSelector` importing back out of `AgentPanel`
 * would be circular.
 */

/**
 * Every tool whose UI is a card, mapped to the name that card goes by in
 * analyst-facing copy. Disabling one of these changes the sandbox+apply
 * workflow rather than just narrowing coverage, which is what `ToolSelector`
 * warns about.
 */
export const CARD_TOOLS = {
  propose_finding: "finding",
  propose_annotation: "annotation",
  propose_story_block: "story block",
  propose_chart: "chart",
} as const;

/**
 * The subset of {@link CARD_TOOLS} whose cards resolve against the
 * `agent-proposals` query, mapped to the `ChatItem` kind `AgentPanel` renders
 * for each. Membership here means all four of: no generic tool row on the live
 * `tool_call`, an item on the live `tool_result`, an item from the persisted
 * transcript, and a proposals refetch once the result lands.
 *
 * `propose_finding` and `propose_chart` are deliberately absent — their cards
 * are built from the *call* args and never touch the proposals query.
 */
export const PROPOSAL_TOOLS = {
  propose_annotation: "proposal",
  propose_story_block: "storyProposal",
} as const;

export type ProposalTool = keyof typeof PROPOSAL_TOOLS;
export type ProposalItemKind = (typeof PROPOSAL_TOOLS)[ProposalTool];

/** Every proposal tool must also be a card tool — checked at compile time. */
const _proposalToolsAreCardTools: Record<ProposalTool, string> = CARD_TOOLS;
void _proposalToolsAreCardTools;

/**
 * The `ChatItem` kind a tool's proposal renders as, or null when the tool is
 * not a proposal tool (the caller then falls back to its generic handling).
 */
export function proposalItemKind(tool: string | null | undefined): ProposalItemKind | null {
  if (!tool) return null;
  return (PROPOSAL_TOOLS as Record<string, ProposalItemKind | undefined>)[tool] ?? null;
}
