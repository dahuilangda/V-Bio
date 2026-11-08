// 领域拆分门面：历史导入路径保持不变，实现按领域拆分至 ./supabase/*。
export * from './supabase/rest';
export * from './supabase/shared';
export * from './supabase/projects';
export * from './supabase/tasksQuery';
export * from './supabase/taskDetail';
export * from './supabase/tasks';
export * from './supabase/shares';
export * from './supabase/copilot';
export * from './supabase/shareLists';
export * from './supabase/tokens';
