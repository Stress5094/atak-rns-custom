package io.github.stress5094.customrnsbridge

import android.content.Context
import android.content.res.ColorStateList
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.core.widget.TextViewCompat
import com.google.android.material.button.MaterialButton
import com.google.android.material.color.MaterialColors

/**
 * Material 3 pieces for screens built in code, so every screen makes them the
 * same way (Google's guidelines, operator 2026-10-22).
 *
 * BUTTON EMPHASIS, highest to lowest, one rule for the whole app:
 *   filled   the action a screen exists for (Save, Accept, Push) -- one per view
 *   tonal    a frequent action that is not the main one
 *   outlined everything else that does something
 *   text     low-priority or "more" actions
 *   danger   destroys or interrupts (Restart, Quit, Reset): outlined, in the
 *            error colour
 * Labels in sentence case; Material 3 buttons are not all caps.
 *
 * TEXT uses Material's type scale (textAppearance*), never under 12 sp, and
 * colours from the theme, so the dark theme and the 4.5:1 contrast rule hold
 * without each screen checking.
 */
object Ui {
    fun attr(c: Context, attr: Int, fallback: Int = 0xFF000000.toInt()) =
        MaterialColors.getColor(c, attr, fallback)

    fun onSurface(c: Context) = attr(c, com.google.android.material.R.attr.colorOnSurface)
    fun onSurfaceVariant(c: Context) = attr(c, com.google.android.material.R.attr.colorOnSurfaceVariant)
    fun error(c: Context) = attr(c, androidx.appcompat.R.attr.colorError)
    fun res(c: Context, id: Int) = ContextCompat.getColor(c, id)

    fun filled(c: Context, label: String) = MaterialButton(c).apply { text = label }

    fun tonal(c: Context, label: String) =
        MaterialButton(c, null, R.attr.tonalButtonStyle).apply { text = label }

    fun outlined(c: Context, label: String) =
        MaterialButton(c, null, com.google.android.material.R.attr.materialButtonOutlinedStyle)
            .apply { text = label }

    fun textButton(c: Context, label: String) =
        MaterialButton(c, null, com.google.android.material.R.attr.borderlessButtonStyle)
            .apply { text = label }

    fun danger(c: Context, label: String) = outlined(c, label).apply {
        val e = error(c)
        setTextColor(e)
        strokeColor = ColorStateList.valueOf(e)
    }

    /** Material type scale roles, as used here. */
    enum class Type(val style: Int) {
        TITLE_LARGE(com.google.android.material.R.style.TextAppearance_Material3_TitleLarge),
        TITLE_MEDIUM(com.google.android.material.R.style.TextAppearance_Material3_TitleMedium),
        TITLE_SMALL(com.google.android.material.R.style.TextAppearance_Material3_TitleSmall),
        BODY_LARGE(com.google.android.material.R.style.TextAppearance_Material3_BodyLarge),
        BODY_MEDIUM(com.google.android.material.R.style.TextAppearance_Material3_BodyMedium),
        BODY_SMALL(com.google.android.material.R.style.TextAppearance_Material3_BodySmall),
        LABEL_LARGE(com.google.android.material.R.style.TextAppearance_Material3_LabelLarge),
        LABEL_MEDIUM(com.google.android.material.R.style.TextAppearance_Material3_LabelMedium),
    }

    /** Apply a type role; the colour stays the theme's unless given. */
    fun style(t: TextView, type: Type, color: Int? = null): TextView {
        TextViewCompat.setTextAppearance(t, type.style)
        t.setTextColor(color ?: onSurface(t.context))
        return t
    }

    /** A section heading: Title small, sentence case, in the primary colour. */
    fun heading(t: TextView) = style(t, Type.TITLE_SMALL,
        attr(t.context, androidx.appcompat.R.attr.colorPrimary))
}
