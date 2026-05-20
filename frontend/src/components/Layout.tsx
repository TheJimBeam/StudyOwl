import { Link, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

/**
 * Authed-route shell: top navbar (greeting + logout) and an <Outlet /> for the
 * actual page. Wrapped around every route behind ProtectedRoute.
 */
export function Layout() {
  const { user, logout } = useAuth()
  const displayName = user?.name ?? (user?.role === 'teacher' ? 'Teacher' : 'Student')

  return (
    <div className="h-screen flex flex-col">
      <nav className="bg-indigo-900 text-white px-3 py-3 sm:p-4 shrink-0 shadow-md z-10">
        <div className="max-w-6xl mx-auto flex justify-between items-center gap-2">
          <Link to="/" className="text-xl sm:text-2xl font-bold whitespace-nowrap">
            🦉 StudyOwl
          </Link>
          <div className="flex items-center gap-2 sm:gap-4 min-w-0">
            <span className="hidden sm:inline text-sm text-indigo-100 truncate max-w-[12rem]">
              Hi, {displayName} 👋
            </span>
            <button
              onClick={logout}
              className="px-3 py-2 sm:px-4 text-sm sm:text-base bg-red-600 hover:bg-red-700 rounded-lg transition whitespace-nowrap"
            >
              Log Out
            </button>
          </div>
        </div>
      </nav>
      <main className="flex-1 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}

export default Layout
