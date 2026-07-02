// user_model.ts - 사용자 도메인 모델과 권한 검사 (TypeScript)
export type Role = "admin" | "editor" | "viewer";

export interface User {
  id: string;
  name: string;
  email: string;
  role: Role;
  createdAt: Date;
}

const PERMISSIONS: Record<Role, string[]> = {
  admin: ["read", "write", "delete", "manage_users"],
  editor: ["read", "write"],
  viewer: ["read"],
};

export function can(user: User, action: string): boolean {
  return PERMISSIONS[user.role].includes(action);
}

export function activeUsers(users: User[], since: Date): User[] {
  return users.filter((u) => u.createdAt >= since);
}

export class UserRepository {
  private users = new Map<string, User>();

  add(user: User): void {
    this.users.set(user.id, user);
  }

  findByEmail(email: string): User | undefined {
    return [...this.users.values()].find((u) => u.email === email);
  }

  countByRole(): Record<Role, number> {
    const counts: Record<Role, number> = { admin: 0, editor: 0, viewer: 0 };
    for (const u of this.users.values()) counts[u.role]++;
    return counts;
  }
}
