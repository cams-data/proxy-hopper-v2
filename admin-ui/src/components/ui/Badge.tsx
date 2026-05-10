import { clsx } from "clsx";

interface BadgeProps {
  children: React.ReactNode;
  variant?: "default" | "success" | "warning" | "danger" | "muted";
  className?: string;
}

export function Badge({ children, variant = "default", className }: BadgeProps) {
  return (
    <span
      className={clsx(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
        variant === "default" &&
          "bg-primary-100 text-primary-800 dark:bg-primary-900/30 dark:text-primary-300",
        variant === "success" &&
          "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
        variant === "warning" &&
          "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
        variant === "danger" &&
          "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
        variant === "muted" &&
          "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400",
        className,
      )}
    >
      {children}
    </span>
  );
}
