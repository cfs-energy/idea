export interface MetricsBackfillStatus {
    state: 'idle' | 'running' | 'completed' | 'failed' | 'interrupted';
    jobs_scanned: number;
    points_sent: number;
    points_built: number;
    points_skipped: number;
    errors: number;
    started_at: string | null;
    finished_at: string | null;
    dry_run: boolean;
    last_error: string | null;
}
