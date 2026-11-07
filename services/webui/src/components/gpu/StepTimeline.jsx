/**
 * Deployment timeline: events in time order, oldest first, so a reader can
 * follow what happened from queue to serving (or to the failure).
 * @param {{events:Array<{ts:string, state:number, message:string}>}} props
 * @returns {JSX.Element}
 */
export default function StepTimeline({ events }) {
  const ordered = [...(events ?? [])].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  if (ordered.length === 0) return <p>No events recorded yet.</p>;
  return (
    <ol className="step-timeline">
      {ordered.map((e, i) => (
        <li key={`${e.ts}-${i}`}>
          <time dateTime={e.ts}>{new Date(e.ts).toLocaleString()}</time> <span>{e.message}</span>
        </li>
      ))}
    </ol>
  );
}
