/**
 * Modal confirmation. Cancel holds the initial focus so Enter never confirms
 * a destructive action by accident.
 * @param {{title:string, body:string, confirmLabel:string, onConfirm:Function, onCancel:Function}} props
 * @returns {JSX.Element}
 */
export default function ConfirmAction({ title, body, confirmLabel, onConfirm, onCancel }) {
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
      <div className="modal-content">
        <h2>{title}</h2>
        <p>{body}</p>
        <div className="form-actions">
          <button type="button" className="btn-secondary" autoFocus onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="btn-primary" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
