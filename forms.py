"""WTForms classes for the account and library editors."""
from flask_wtf import FlaskForm
from wtforms import (BooleanField, IntegerField, PasswordField, SelectField,
                     StringField)
from wtforms.validators import DataRequired, NumberRange, Optional


class AccountForm(FlaskForm):
    """New-account form. The library card fields are optional here because
    access-code libraries don't use them; the route requires them for
    EZproxy libraries.

    NYT credentials aren't here at all — cookies captured via the
    dashboard's noVNC capture flow are the auth path; the renewer
    doesn't read NYT username/password."""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[Optional()])
    library_password = PasswordField('Library Password/PIN', validators=[Optional()])
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[Optional(), NumberRange(min=1)])
    active = BooleanField('Active', default=True)


class EditAccountForm(FlaskForm):
    """Edit-account form. library_password is OPTIONAL — leaving it blank
    keeps the existing encrypted value; the route ignores empty submissions.
    As in AccountForm, the route decides whether the card fields are needed."""
    name = StringField('Account Name', validators=[DataRequired()])
    library_type = SelectField('Library Type', choices=[])
    library_username = StringField('Library Username/Card Number', validators=[Optional()])
    library_password = PasswordField('Library Password/PIN', validators=[Optional()])
    renewal_interval = IntegerField('Renewal Interval Override (hours)', validators=[Optional(), NumberRange(min=1)])
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
        description='Your library\'s EZproxy NYT URL, or an nytimes.com/subscription/redeem access-code link',
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
