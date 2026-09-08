import type { ReactNode } from 'react'

/** 统一的状态区: loading / empty / error —— 每个数据视图都必须有这三种。 */
export function StateBlock({
  kind,
  title,
  children,
}: {
  kind: 'loading' | 'empty' | 'error'
  title: string
  children?: ReactNode
}) {
  return (
    <div className={`state${kind === 'error' ? ' error' : ''}`}>
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
    </div>
  )
}
