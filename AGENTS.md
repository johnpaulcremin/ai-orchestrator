Use AGENTS.md strictly.



First, run only:

git status --short

git diff --stat



Then make a short plan. Do not edit files yet.



Goal:

\[describe one specific feature or bug]



Constraints:

\- Keep the change minimal.

\- Touch only the smallest necessary files.

\- Do not modify frontend unless required.

\- Do not add dependencies.

\- Use rg/fd/ast-grep for search instead of scanning the repo.

\- Run only the relevant checks before finishing.

