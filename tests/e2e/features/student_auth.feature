Feature: Student Authentication
  As an enrolled student
  I want to log in with my generated credentials
  So that I can access the student portal

  Background:
    Given the teacher account exists in the database
    And the E2E test subject exists
    And the student is enrolled in the E2E test subject

  @smoke @auth
  Scenario: Student logs in with generated credentials
    Given I have the student's generated credentials
    And I am on the login page
    When I submit the login form with the student credentials
    Then I should be redirected to the student portal
