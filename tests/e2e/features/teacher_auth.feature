Feature: Teacher Authentication
  As a teacher seeded in the database
  I want to log in to the system
  So that I can manage subjects and students

  Background:
    Given the teacher account exists in the database

  @smoke @auth
  Scenario: Successful teacher login
    Given I am on the login page
    When I submit the login form with the teacher credentials
    Then I should be redirected to the teacher dashboard

  @auth @sad-path
  Scenario: Failed teacher login with wrong password
    Given I am on the login page
    When I submit the login form with username "e2e_teacher" and password "wrongpassword"
    Then I should see a login error message
    And I should remain on the login page
