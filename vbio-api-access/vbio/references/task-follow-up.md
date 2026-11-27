# Task follow-up

The Builder uses `GET /status/<task_id>` and `GET /results/<task_id>`. Virtual Screening also exposes `GET /results/<task_id>/screening`. Every route includes the `project_id` query parameter and the `X-API-Token` header. A failed submission can be ambiguous after a timeout or connection drop. Inspect the project task list for the original task before sending another POST.

### Browser link

The frontend route is `<V-Bio web base>/projects/<project_id>/tasks`. Use the web address from a user-supplied V-Bio link or a verified deployment setting. Preserve the configured web base path and URL-encode the project ID. The API Access gateway can live on a different origin, so an API base alone does not establish a browser URL. If the web address is unknown, give the project name or ID, the task ID, and instructions to open the project's **Tasks** page. Link to the task list and tell the user which task ID to locate. Keep credentials out of every link.

### Track and download each task

Run the client from the user's current workspace. The client keeps a local record in `.vbio/tasks.sqlite3` scoped by gateway, project and task ID. It stores the submitted job name, last observed state and time, and any downloaded ZIP path. It does not store the project token. `tasks` lists these records offline, including whether a saved path still exists. Use the project Tasks page for jobs outside the local history. To refresh, use `status <task_id>` with the original connection. When asked about several jobs, refresh those jobs and report one row per task with the name or ID, status, time estimate if available, and result path. Clearly label older observations when no fresh check was possible.

Use the server's top-level `state`. `PENDING` is waiting. `STARTED` and `PROGRESS` are running. `SUCCESS`, `FAILURE`, and `REVOKED` are terminal. Only `SUCCESS` triggers automatic download. Use `info.message`, `info.status` or `info.exc_message` for the stage or reason. Use `info.estimated_remaining_seconds` when available and report it as V-Bio's estimate. Without it, say when the task was submitted if recorded, and show any available stage or progress. Percent complete alone cannot predict time remaining.

After showing the user the submission receipt, run `wait <task_id> [output.zip]` for each task the user wants followed. It checks every 15 seconds by default and saves the ZIP when available. It retries a missing archive after success and transient read errors within its wait budget. The default budget is 15 minutes per invocation. Continue waiting on the same task when requested and the session permits, or report the last state and how to resume. A wait ending, a network error, or an archive still publishing does not change the server's task state. Never resubmit in order to resume a read.

The default destination is `vbio-results/vbio-<task_id>.zip` in the current workspace. Use the user's destination if provided. Report success and download as separate facts. Give one saved file link per task, and preserve any existing file. For a remote session, offer a downloadable artifact link when the harness supports it. Keep drafts under `.vbio/drafts/` and leave original inputs intact. In a later session, use `tasks` to recover the IDs and paths, then obtain the original connection if a fresh read is needed.

## Match the requested action

- For a status check, use `status <task_id>` and report the current state.
- For monitoring, use `wait <task_id>` when download is wanted. When the user wants only to wait, poll status and stop at a terminal state.
- For downloads limited to completed jobs, check status and call `results <task_id> [output.zip]` for successful jobs.
- After a new submission, default to monitoring and downloading unless the user gives different instructions.

Report a task ID and a known task-list link in the submission receipt. For a single status check, give the state and any available stage, estimate, or failure reason. If the archive is still publishing, report computation success and archive availability separately. For multiple tasks, use a table with one row per task. Offer the next action only when there is a decision or blocker.
