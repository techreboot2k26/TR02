export type UserRole = 'STUDENT' | 'STAFF' | 'ADMIN';

export interface User {
  id: string;
  name: string;
  email: string;
  role: UserRole;
  created_at: string;
}

export type CounterStatus = 'OPEN' | 'CLOSED' | 'BUSY' | 'MAINTENANCE';

export interface Service {
  id: string;
  name: string;
  code: string;
  description?: string;
  created_at: string;
}

export interface Counter {
  id: string;
  service_id: string;
  service_name?: string;
  service_code?: string;
  name: string;
  status: CounterStatus;
  assigned_staff_id?: string;
  assigned_staff_name?: string;
  created_at: string;
}

export type TokenPriority = 'NORMAL' | 'HIGH' | 'PRIORITY' | 'URGENT';

export type TokenStatus =
  | 'WAITING'
  | 'SERVING'
  | 'HELD'
  | 'COMPLETED'
  | 'SKIPPED'
  | 'CANCELLED'
  | 'WAITLISTED'
  | 'PROMOTED'
  | 'EXPIRED';

export interface Token {
  id: string;
  token_number: string;
  student_id?: string;
  student_name: string;
  student_email?: string;
  service_id: string;
  service_name?: string;
  counter_id?: string;
  counter_name?: string;
  priority: TokenPriority;
  status: TokenStatus;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  skipped_at?: string | null;
  held_at?: string | null;
  notes?: string | null;
  wait_duration_seconds?: number;
  serving_duration_seconds?: number;
}

export interface OperationalStats {
  queue_length: number;
  currently_serving_number?: string | null;
  waiting_count: number;
  held_count: number;
  completed_today_count: number;
  avg_service_time_minutes: number;
}

export interface StaffDashboardData {
  staff: User;
  counter: Counter;
  service: Service;
  current_token: Token | null;
  waiting_queue: Token[];
  stats: OperationalStats;
}

export interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
}

export interface ToastMessage {
  id: string;
  type: 'success' | 'error' | 'info' | 'warning';
  title: string;
  message: string;
}
