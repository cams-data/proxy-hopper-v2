import { useState, useRef, useEffect, useId } from "react";
import { ChevronDown, Check } from "lucide-react";
import { clsx } from "clsx";

interface ComboboxProps {
  options: string[];
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
}

export function Combobox({
  options,
  value,
  onChange,
  placeholder = "Search…",
  required,
  disabled,
}: ComboboxProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const id = useId();

  const filtered = query
    ? options.filter((o) => o.toLowerCase().includes(query.toLowerCase()))
    : options;

  function select(opt: string) {
    onChange(opt);
    setQuery("");
    setOpen(false);
  }

  function handleBlur() {
    // Discard typed text if it doesn't match an existing option
    if (!options.includes(value)) {
      onChange("");
    }
    setQuery("");
    setOpen(false);
  }

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!containerRef.current?.contains(e.target as Node)) {
        handleBlur();
      }
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open, value, options]);

  const displayValue = open ? query : value;

  return (
    <div ref={containerRef} className="relative flex-1">
      <div className="relative">
        <input
          ref={inputRef}
          id={id}
          role="combobox"
          aria-expanded={open}
          aria-autocomplete="list"
          value={displayValue}
          placeholder={placeholder}
          required={required}
          disabled={disabled}
          onChange={(e) => {
            setQuery(e.target.value);
            // Only propagate if it matches an option; otherwise hold "" so form stays invalid
            const match = options.find(
              (o) => o.toLowerCase() === e.target.value.toLowerCase(),
            );
            onChange(match ?? "");
            if (!open) setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={handleBlur}
          onKeyDown={(e) => {
            if (e.key === "Escape") { setOpen(false); setQuery(""); }
            if (e.key === "Enter" && filtered.length === 1) {
              e.preventDefault();
              select(filtered[0]);
            }
          }}
          className="w-full rounded-md border border-gray-200 bg-white py-1.5 pl-3 pr-8 text-sm text-gray-900 placeholder-gray-400 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100"
        />
        <button
          type="button"
          tabIndex={-1}
          disabled={disabled}
          onClick={() => { setOpen((o) => !o); inputRef.current?.focus(); }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400"
        >
          <ChevronDown className="h-4 w-4" />
        </button>
      </div>

      {open && (
        <ul
          role="listbox"
          className="absolute z-50 mt-1 max-h-48 w-full overflow-auto rounded-md border border-gray-200 bg-white py-1 shadow-md dark:border-gray-700 dark:bg-gray-800"
        >
          {filtered.length > 0 ? (
            filtered.map((opt) => (
              <li
                key={opt}
                role="option"
                aria-selected={opt === value}
                onPointerDown={(e) => { e.preventDefault(); select(opt); }}
                className={clsx(
                  "flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm",
                  opt === value
                    ? "bg-primary-50 text-primary-700 dark:bg-primary-900/20 dark:text-primary-300"
                    : "text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700",
                )}
              >
                <Check className={clsx("h-3.5 w-3.5 shrink-0", opt === value ? "opacity-100" : "opacity-0")} />
                {opt}
              </li>
            ))
          ) : (
            <li className="px-3 py-2 text-sm text-gray-400">No providers found</li>
          )}
        </ul>
      )}
    </div>
  );
}
