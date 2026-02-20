interface LeadOptHeaderProps {
  targetChain: string;
  ligandChain: string;
}

export function LeadOptHeader({
  targetChain,
  ligandChain
}: LeadOptHeaderProps) {
  return (
    <section className="panel subtle lead-opt-head">
      <div className="lead-opt-panel-head">
        <h3>Lead Optimization</h3>
        <div className="row">
          <span className="badge">T: {targetChain}</span>
          <span className="badge">L: {ligandChain}</span>
        </div>
      </div>
    </section>
  );
}
