import { useStore } from "../store";

/** Under the chat header, on an outsider's PR (or when the check failed): connector lookups ask first there. */
export function OutsideAuthorStrip() {
  const trust = useStore((s) => s.trust);
  if (!trust || trust.state === "trusted") return null;
  return (
    <div className="flash flash-warn chat-trust" role="note">
      {trust.state === "outside"
        ? `Outside author: @${trust.author} isn't a member or collaborator of this repo. Jira, Slack and other-repo lookups ask first.`
        : "Couldn't check the author; lookups ask first."}
    </div>
  );
}

/** "outside author", next to the author's name in the PR header. */
export function OutsideAuthorLabel() {
  const trust = useStore((s) => s.trust);
  if (trust?.state !== "outside") return null;
  return (
    <span
      className="Label Label--attention"
      title={`@${trust.author} isn't a member or collaborator of this repo. Jira, Slack and other-repo lookups ask first.`}
    >
      outside author
    </span>
  );
}
