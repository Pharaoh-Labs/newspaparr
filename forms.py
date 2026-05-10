"""WTForms classes for the account and library editors."""
from flask_wtf import FlaskForm
from wtforms import (BooleanField, IntegerField, PasswordField, SelectField,
                     StringField, TextAreaField)
from wtforms.validators import DataRequired, NumberRange


class AccountForm(FlaskForm):
    """New-account form. library_password is required.

    NYT credentials aren't here at all — cookies captured via the
    dashboard's noVNC capture flow are the auth path; the renewer
    doesn't read NYT username/password."""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[DataRequired()])
    library_password = PasswordField('Library Password/PIN', validators=[DataRequired()])
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[])
    active = BooleanField('Active', default=True)


class EditAccountForm(FlaskForm):
    """Edit-account form. library_password is OPTIONAL — leaving it blank
    keeps the existing encrypted value; the route ignores empty submissions."""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[DataRequired()])
    library_password = PasswordField('Library Password/PIN', validators=[])
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[])
    active = BooleanField('Active', default=True)


class LibraryForm(FlaskForm):
    """Library configuration — the EZproxy NYT URL and a couple defaults."""
    name = StringField('Library Name', validators=[DataRequired()])
    type = SelectField('Library Type', choices=[
        ('generic_oclc', 'OCLC Library'),
        ('custom', 'Custom Library'),
    ])
    nyt_url = StringField(
        'NYT Access URL',
        validators=[DataRequired()],
        description='Direct URL for NYT access through your library',
    )
    homepage = StringField(
        'Library Homepage (optional)',
        description='Main library website URL for linking',
    )
    default_renewal_hours = IntegerField(
        'Default Renewal Hours',
        validators=[NumberRange(min=1, max=168)],
        default=24,
        description='Renewals will run at this interval + 1 minute',
    )
    active = BooleanField('Active', default=True)
    custom_config = TextAreaField(
        'Additional Configuration (JSON)',
        description='Optional: JSON configuration for advanced settings',
    )
