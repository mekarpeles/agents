"""agents — shell tab-completion scripts.

`agents completion {zsh,bash}` prints a script to source; it calls the hidden
`agents _complete <kind>` for the dynamic candidates (registered names, groups,
types) so completion always reflects the live registry. No runtime dependency
(no argcomplete) — just a shell function that shells back into `agents`.
"""

# The subcommands that take an agent name as their first positional (so <TAB>
# there should offer registered agent names). Kept in sync with cli's parser.
_NAME_CMDS = "up down attach detach send rm a d"

ZSH = r"""#compdef agents
# compadd directly (rather than _arguments) so a command that takes an agent name
# offers only agents — never a fall-through to filename completion when the
# registry is empty or the lookup returns nothing.
_agents() {
  local cmd prev
  if (( CURRENT == 2 )); then
    compadd -- register up down attach detach send list find whoami rm migrate \
               upgrade configure completion help
    return
  fi
  cmd=$words[2]
  prev=$words[CURRENT-1]
  case $prev in
    -g|--group) compadd -- ${(f)"$(agents _complete groups 2>/dev/null)"}; return ;;
    -t|--type)  compadd -- ${(f)"$(agents _complete types 2>/dev/null)"};  return ;;
  esac
  case $cmd in
    down|send)   compadd -- ${(f)"$(agents _complete targets 2>/dev/null)"} ;;
    up|attach|detach|rm|a|d)
      compadd -- ${(f)"$(agents _complete names 2>/dev/null)"} ;;
  esac
}
compdef _agents agents
"""

BASH = r"""_agents() {
  local cur prev cmd
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev="${COMP_WORDS[COMP_CWORD-1]}"
  cmd="${COMP_WORDS[1]}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "register up down attach detach send list find whoami rm migrate upgrade configure completion help" -- "$cur") )
    return
  fi
  case "$prev" in
    -g|--group) COMPREPLY=( $(compgen -W "$(agents _complete groups 2>/dev/null)" -- "$cur") ); return ;;
    -t|--type)  COMPREPLY=( $(compgen -W "$(agents _complete types 2>/dev/null)" -- "$cur") ); return ;;
  esac
  case "$cmd" in
    down|send)   COMPREPLY=( $(compgen -W "$(agents _complete targets 2>/dev/null)" -- "$cur") ) ;;
    up|attach|detach|rm|a|d)
      COMPREPLY=( $(compgen -W "$(agents _complete names 2>/dev/null)" -- "$cur") ) ;;
  esac
}
complete -F _agents agents
"""

SCRIPTS = {"zsh": ZSH, "bash": BASH}


def script(shell):
    return SCRIPTS[shell]
