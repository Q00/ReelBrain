type BrainMarkProps = { compact?: boolean };

export function BrainMark({ compact = false }: BrainMarkProps) {
  return (
    <span className={`brain-mark ${compact ? "brain-mark--compact" : ""}`} aria-hidden="true">
      <span className="brain-dot brain-dot--one" />
      <span className="brain-dot brain-dot--two" />
      <span className="brain-dot brain-dot--three" />
      <span className="brain-dot brain-dot--four" />
      <span className="brain-dot brain-dot--five" />
      <span className="brain-core" />
    </span>
  );
}
