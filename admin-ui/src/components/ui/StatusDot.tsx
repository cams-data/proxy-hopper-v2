import { clsx } from "clsx";

export type StatusDotStatus = "healthy" | "unhealthy" | "unknown";

interface StatusDotProps {
  status: StatusDotStatus;
  className?: string;
  title?: string;
}

/** Small colored dot for at-a-glance liveness — green/red/grey. */
export function StatusDot({ status, className, title }: StatusDotProps) {
  return (
    <span
      title={title}
      className={clsx(
        "h-1.5 w-1.5 shrink-0 rounded-full",
        status === "healthy" && "bg-green-500",
        status === "unhealthy" && "bg-red-500",
        status === "unknown" && "bg-gray-300 dark:bg-gray-600",
        className,
      )}
    />
  );
}
