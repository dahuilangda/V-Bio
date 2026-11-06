-- 运行时任务 id 的唯一部分索引：gateway 快照插入与 UI DRAFT 行挂接 task_id
-- 都依赖它去重（task_store.resolution=ignore-duplicates）。存量库若已有重复
-- task_id，先保留每组最新一行再建索引。
DELETE FROM public.project_tasks a
USING public.project_tasks b
WHERE a.task_id <> ''
  AND a.task_id = b.task_id
  AND a.ctid < b.ctid;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_project_tasks_task_id
  ON public.project_tasks (task_id) WHERE task_id <> '';
