import type { ButtonHTMLAttributes, ReactNode } from "react";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "sm" | "md";

interface BaseProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "title"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: ReactNode;
}

interface LabeledProps extends BaseProps {
  children: ReactNode;
  iconOnly?: false;
  /** Optional native tooltip; falls back to the visible label when omitted. */
  title?: string;
}

interface IconOnlyProps extends BaseProps {
  children?: undefined;
  iconOnly: true;
  /** Required for icon-only buttons -- there's no visible label for a11y/tooltip to fall back to. */
  "aria-label": string;
  title?: string;
}

export type ButtonProps = LabeledProps | IconOnlyProps;

/**
 * The one button every control in the app should render through, so nothing
 * on screen mixes sizes/padding/radius (see --control-h-sm/md in App.css).
 * Icon-only buttons require aria-label and get a matching native `title`
 * tooltip for free unless one is explicitly given.
 */
export function Button({
  variant = "secondary",
  size = "sm",
  icon,
  iconOnly,
  className,
  children,
  title,
  ...rest
}: ButtonProps) {
  const classes = ["btn", `btn-${variant}`, `btn-${size}`, iconOnly ? "btn-icon" : "", className]
    .filter(Boolean)
    .join(" ");

  const resolvedTitle = title ?? (iconOnly ? rest["aria-label"] : undefined);

  return (
    <button className={classes} title={resolvedTitle} {...rest}>
      {icon}
      {iconOnly ? null : children}
    </button>
  );
}
